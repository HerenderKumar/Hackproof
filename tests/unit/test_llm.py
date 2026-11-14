"""The one client for model calls (section 15, D-037): request shape, outcomes, transports, rows."""

import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core import queue
from core.db import models
from core.errors import InfraError
from core.llm import (
    LLMClient,
    LLMOutputError,
    ReplayMissing,
    Tool,
    Transport,
    live_transport,
    make_llm_client,
    recording_transport,
    replay_transport,
    request_key,
)
from core.settings import Settings

Sessions = async_sessionmaker[AsyncSession]
BASE = "https://gateway.example/v1"
KEY = "k-secret-123"


class Answer(BaseModel):
    sum: int


TOOL = Tool("submit_answer", "Submit the answer.", Answer)


def reply(
    arguments: str = '{"sum": 42}', name: str = "submit_answer", finish: str = "tool_calls"
) -> dict[str, Any]:
    call = {"id": "call_1", "type": "function", "function": {"name": name, "arguments": arguments}}
    return {
        "id": "gen-1",
        "model": "claude-sonnet-5-5",
        "choices": [
            {"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": None,
                                                                "tool_calls": [call]}}
        ],
        "usage": {"prompt_tokens": 390, "completion_tokens": 50, "cost": 0.14},
    }  # fmt: skip


def text_only() -> dict[str, Any]:
    data = reply()
    data["choices"][0]["message"] = {"role": "assistant", "content": "It is 42."}
    data["choices"][0]["finish_reason"] = "stop"
    return data


def broken_call() -> dict[str, Any]:
    data = reply()
    data["choices"][0]["message"]["tool_calls"] = [{"id": "call_1", "function": None}, 7]
    return data


class Scripted:
    """A transport that answers from a list and keeps every request body it was sent."""

    def __init__(self, *replies: dict[str, Any] | Exception) -> None:
        self.replies = list(replies)
        self.bodies: list[dict[str, Any]] = []

    async def __call__(self, body: dict[str, Any]) -> dict[str, Any]:
        self.bodies.append(body)
        answer = self.replies.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


async def ask(transport: Transport, **overrides: Any) -> Answer:
    values: dict[str, Any] = {
        "purpose": "draft",
        "model": "m",
        "system": "Answer only by calling submit_answer.",
        "user": "What is 17 + 25?",
        "tool": TOOL,
        "max_tokens": 100,
    }
    return await LLMClient(transport).submit(**{**values, **overrides})


# --- the request and the reply -------------------------------------------------------------------


async def test_a_tool_call_comes_back_as_the_tools_model() -> None:
    assert await ask(Scripted(reply())) == Answer(sum=42)


async def test_the_request_names_the_tool_and_sends_no_temperature() -> None:
    transport = Scripted(reply())

    await ask(transport)

    assert transport.bodies == [
        {
            "model": "m",
            "max_tokens": 100,
            "messages": [
                {"role": "system", "content": "Answer only by calling submit_answer."},
                {"role": "user", "content": "What is 17 + 25?"},
            ],
            "tools": [TOOL.definition()],
            "tool_choice": "auto",
        }
    ]
    assert TOOL.definition() == {
        "type": "function",
        "function": {
            "name": "submit_answer",
            "description": "Submit the answer.",
            "parameters": Answer.model_json_schema(),
        },
    }


UNUSABLE = {
    "text_only": (text_only(), "did not call submit_answer"),
    "other_tool": (reply(name="submit_other"), "submit_other"),
    "broken_call": (broken_call(), "did not call submit_answer"),
    "not_json": (reply(arguments='{"sum": 4'), "not valid JSON"),
    "wrong_schema": (reply(arguments='{"total": 42}'), "did not match"),
    "length": (reply(arguments='{"sum": 4', finish="length"), "cut off"),
    "content_filter": (reply(finish="content_filter"), "declined"),
}


@pytest.mark.parametrize("bad", list(UNUSABLE))
async def test_unusable_replies_are_output_errors(bad: str) -> None:
    answer, words = UNUSABLE[bad]
    with pytest.raises(LLMOutputError, match=words):
        await ask(Scripted(answer))


# --- the live transport --------------------------------------------------------------------------


def gateway(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_live_transport_posts_with_the_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=reply())

    send = live_transport(BASE, KEY, 5, client=gateway(handler))

    assert await send({"model": "m"}) == reply()
    (request,) = seen
    assert (request.method, str(request.url)) == ("POST", f"{BASE}/chat/completions")
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert json.loads(request.content) == {"model": "m"}


def failing(failure: str | int) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        if failure == "connect":
            raise httpx.ConnectError("refused", request=request)
        assert isinstance(failure, int)
        return httpx.Response(failure, text=f"error for Bearer {KEY}")

    return handler


@pytest.mark.parametrize("failure", ["timeout", "connect", 429, 500, 400, 401])
async def test_gateway_failures_are_infra_errors(failure: str | int) -> None:
    send = live_transport(BASE, KEY, 5, client=gateway(failing(failure)))

    with pytest.raises(InfraError) as caught:
        await send({"model": "m"})

    assert KEY not in str(caught.value)
    if isinstance(failure, int):
        assert str(failure) in str(caught.value)


@pytest.mark.parametrize(
    "body", [b"<html>Bad gateway</html>", b'{"error": {"message": "overloaded"}}']
)
async def test_a_malformed_gateway_reply_is_an_infra_error(body: bytes) -> None:
    send = live_transport(BASE, KEY, 5, client=gateway(lambda r: httpx.Response(200, content=body)))

    with pytest.raises(InfraError):
        await LLMClient(send).submit(
            purpose="draft", model="m", system="s", user="u", tool=TOOL, max_tokens=10
        )


# --- record and replay ---------------------------------------------------------------------------


async def test_a_recording_replays_without_the_network(tmp_path: Path) -> None:
    body = {"model": "m", "messages": []}
    record = recording_transport(Scripted(reply()), tmp_path)

    assert await record(body) == reply()
    saved = json.loads((tmp_path / f"{request_key(body)}.json").read_text())
    assert saved == {"request": body, "response": reply()}
    assert await replay_transport(tmp_path)(body) == reply()


async def test_a_missing_recording_names_its_key(tmp_path: Path) -> None:
    body = {"model": "m", "messages": [{"role": "user", "content": "new"}]}
    with pytest.raises(ReplayMissing, match=request_key(body)):
        await replay_transport(tmp_path)(body)


async def test_the_key_never_reaches_a_recording_or_an_error(tmp_path: Path) -> None:
    ok = live_transport(BASE, KEY, 5, client=gateway(lambda r: httpx.Response(200, json=reply())))
    await LLMClient(recording_transport(ok, tmp_path)).submit(
        purpose="draft", model="m", system="s", user="u", tool=TOOL, max_tokens=10
    )
    refused = live_transport(BASE, KEY, 5, client=gateway(failing(401)))

    with pytest.raises(InfraError) as caught:
        await refused({"model": "m"})

    (recording,) = tmp_path.iterdir()
    assert KEY not in recording.read_text()
    assert KEY not in str(caught.value)


async def test_the_replay_mode_client_needs_no_key(tmp_path: Path) -> None:
    client = make_llm_client(
        Settings(llm_mode="replay", llm_api_key=None, llm_replay_dir=tmp_path), None
    )
    with pytest.raises(ReplayMissing):
        await client.submit(purpose="draft", model="m", system="s", user="u", tool=TOOL,
                            max_tokens=10)  # fmt: skip


# --- llm_calls -----------------------------------------------------------------------------------


async def calls(sessions: Sessions) -> list[models.LlmCall]:
    async with sessions() as session:
        return list(await session.scalars(select(models.LlmCall).order_by(models.LlmCall.id)))


async def test_every_call_writes_an_llm_calls_row(sessions: Sessions) -> None:
    async with sessions.begin() as session:
        job_id, _ = await queue.enqueue(session, "draft_specs", f"draft:{uuid.uuid4()}:1")
    client = LLMClient(
        Scripted(reply(), reply(arguments="{}"), InfraError("down")), sessions=sessions
    )
    ask_again = {"purpose": "draft", "model": "m", "system": "s", "user": "u", "tool": TOOL,
                 "max_tokens": 10, "job_id": job_id}  # fmt: skip

    await client.submit(**ask_again)
    with pytest.raises(LLMOutputError):
        await client.submit(**ask_again)
    with pytest.raises(InfraError):
        await client.submit(**ask_again)

    rows = await calls(sessions)
    assert [(r.ok, r.input_tokens, r.output_tokens) for r in rows] == [
        (True, 390, 50),
        (False, 390, 50),
        (False, None, None),
    ]
    assert all((r.job_id, r.purpose, r.model) == (job_id, "draft", "m") for r in rows)
    assert all(r.duration_ms is not None and r.duration_ms >= 0 for r in rows)
