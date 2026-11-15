"""The model door (Prune design, B1), against a fake Prune."""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from door.app import DoorConfig, make_app

TOKEN = "run-token-1234567890"
SECRET_PROMPT = "the team's private prompt text"
SECRET_REPLY = "the model's private reply text"


def config(tmp_path: Path, **changes: Any) -> DoorConfig:
    values: dict[str, Any] = {
        "token": TOKEN,
        "prune_base_url": "https://prune.test",
        "prune_api_key": "prune_live_key",
        "models": ("gpt-4.1-nano", "claude-haiku-4-5"),
        "run_id": "attempt-1",
        "run_budget_usd": "0.05",
        "max_calls": 3,
        "customer_id": "team-t1",
        "feature_id": "final_run",
        "log_path": tmp_path / "log.jsonl",
        "max_body_bytes": 4096,
    }
    return DoorConfig(**{**values, **changes})


async def _aiter(chunks: list[bytes]) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


class FakePrune:
    def __init__(self, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.seen: list[httpx.Request] = []
        self.status, self.headers = status, headers or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        body = json.loads(request.content)
        if body.get("stream"):
            chunks = [b'data: {"a": 1}\n\n', b'data: {"a": 2}\n\n', b"data: [DONE]\n\n"]
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=_aiter(chunks)
            )
        reply = {
            "choices": [{"message": {"content": SECRET_REPLY}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3},
            "prune_metadata": {
                "cost_with_prune_usd": 0.0001,
                "cache_hit": False,
                "optimizations_applied": [],
            },
        }
        return httpx.Response(self.status, json=reply, headers=self.headers)


@pytest.fixture
def prune() -> FakePrune:
    return FakePrune()


async def client(tmp_path: Path, prune: Any, **changes: Any) -> httpx.AsyncClient:
    transport = httpx.MockTransport(prune)
    app = make_app(config(tmp_path, **changes), transport)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://model-door:8080"
    )


def chat(model: str = "gpt-4.1-nano", **extra: Any) -> dict[str, Any]:
    return {"model": model, "messages": [{"role": "user", "content": SECRET_PROMPT}], **extra}


AUTH = {"Authorization": f"Bearer {TOKEN}"}


def log(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "log.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


async def test_a_chat_call_is_forwarded_to_prune_with_the_run_s_terms(
    tmp_path: Path, prune: FakePrune
) -> None:
    async with await client(tmp_path, prune) as c:
        r = await c.post(
            "/v1/chat/completions",
            json=chat(),
            headers={
                **AUTH,
                "X-Prune-Cache": "semantic",
                "X-Prune-Model-Policy": "balanced",
                "X-Prune-Run-Budget-Usd": "99",
                "X-Team-Header": "kept?",
            },
        )
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == SECRET_REPLY
    (sent,) = prune.seen
    assert str(sent.url) == "https://prune.test/v1/chat/completions"
    h = sent.headers
    assert h["authorization"] == "Bearer prune_live_key" and TOKEN not in str(h.raw)
    assert (h["x-prune-cache"], h["x-prune-run-id"], h["x-prune-run-budget-usd"]) == (
        "off",
        "attempt-1",
        "0.05",
    )
    assert (h["x-prune-max-steps"], h["x-prune-customer-id"], h["x-prune-feature-id"]) == (
        "3",
        "team-t1",
        "final_run",
    )
    assert "x-prune-model-policy" not in h and "x-team-header" not in h
    assert json.loads(sent.content) == chat()


async def test_the_anthropic_route_takes_the_token_as_x_api_key(
    tmp_path: Path, prune: FakePrune
) -> None:
    async with await client(tmp_path, prune) as c:
        r = await c.post(
            "/v1/messages",
            json={**chat("claude-haiku-4-5"), "max_tokens": 5},
            headers={"x-api-key": TOKEN, "anthropic-version": "2023-06-01"},
        )
    assert r.status_code == 200
    (sent,) = prune.seen
    assert sent.url.path == "/v1/messages" and sent.headers["anthropic-version"] == "2023-06-01"


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"x-api-key": "wrong"}])
async def test_a_call_without_the_run_s_token_is_refused(
    tmp_path: Path, prune: FakePrune, headers: dict[str, str]
) -> None:
    async with await client(tmp_path, prune) as c:
        r = await c.post("/v1/chat/completions", json=chat(), headers=headers)
    assert r.status_code == 401 and prune.seen == []


async def test_a_model_not_on_the_list_is_refused_in_words(
    tmp_path: Path, prune: FakePrune
) -> None:
    async with await client(tmp_path, prune) as c:
        r = await c.post("/v1/chat/completions", json=chat("gpt-5.5-pro"), headers=AUTH)
    assert r.status_code == 400 and prune.seen == []
    assert "gpt-5.5-pro is not allowed" in r.json()["error"]["message"]
    assert r.headers["x-door-refusal"] == "model_not_allowed"


async def test_a_body_over_the_cap_is_refused(tmp_path: Path, prune: FakePrune) -> None:
    async with await client(tmp_path, prune) as c:
        r = await c.post("/v1/chat/completions", json=chat(pad="x" * 5000), headers=AUTH)
    assert r.status_code == 413 and prune.seen == []


async def test_calls_past_the_cap_are_refused_by_the_door(tmp_path: Path, prune: FakePrune) -> None:
    async with await client(tmp_path, prune, max_calls=2) as c:
        codes = [
            (await c.post("/v1/chat/completions", json=chat(), headers=AUTH)).status_code
            for _ in range(4)
        ]
    assert codes == [200, 200, 429, 429] and len(prune.seen) == 2


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/"),
        ("POST", "/v1/embeddings"),
        ("POST", "/v1/models"),
        ("GET", "/v1/chat/completions"),
    ],
)
async def test_every_other_route_is_refused(
    tmp_path: Path, prune: FakePrune, method: str, path: str
) -> None:
    async with await client(tmp_path, prune) as c:
        r = await c.request(method, path, headers=AUTH, json={"model": "gpt-4.1-nano"})
    assert r.status_code in (404, 405) and prune.seen == []


async def test_the_model_list_is_answered_by_the_door(tmp_path: Path, prune: FakePrune) -> None:
    async with await client(tmp_path, prune) as c:
        r = await c.get("/v1/models", headers=AUTH)
    assert [m["id"] for m in r.json()["data"]] == ["gpt-4.1-nano", "claude-haiku-4-5"]
    assert prune.seen == []


async def test_a_stream_is_passed_through(tmp_path: Path, prune: FakePrune) -> None:
    async with await client(tmp_path, prune) as c, c.stream(
        "POST", "/v1/chat/completions", json=chat(stream=True), headers=AUTH
    ) as r:
        body = b"".join([chunk async for chunk in r.aiter_raw()])
    assert r.status_code == 200 and body.endswith(b"data: [DONE]\n\n") and body.count(b"data:") == 3
    (line,) = log(tmp_path)
    assert line["stream"] is True and line["bytes"] == len(body)


async def test_the_log_holds_what_was_used_and_never_the_text(tmp_path: Path) -> None:
    prune = FakePrune(status=429, headers={"X-Prune-Shield-Code": "run_budget"})
    async with await client(tmp_path, prune) as c:
        r = await c.post("/v1/chat/completions", json=chat(), headers=AUTH)
        await c.post("/v1/chat/completions", json=chat("not-allowed"), headers=AUTH)
    assert r.status_code == 429 and r.headers["x-prune-shield-code"] == "run_budget"
    first, second = log(tmp_path)
    assert (first["status"], first["shield_code"], first["model"]) == (
        429,
        "run_budget",
        "gpt-4.1-nano",
    )
    assert (
        first["prompt_tokens"],
        first["completion_tokens"],
        first["prune_cost_with_prune_usd"],
    ) == (11, 3, 0.0001)
    assert second["refused"] == "model_not_allowed"
    text = (tmp_path / "log.jsonl").read_text()
    assert SECRET_PROMPT not in text and SECRET_REPLY not in text


async def test_prune_out_of_reach_is_a_502_the_worker_can_tell_apart(tmp_path: Path) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    async with await client(tmp_path, down) as c:
        r = await c.post("/v1/chat/completions", json=chat(), headers=AUTH)
    assert r.status_code == 502 and r.headers["x-door-refusal"] == "unreachable"
    (line,) = log(tmp_path)
    assert line["unreachable"] == "ConnectError"


def test_the_config_reads_the_environment(tmp_path: Path) -> None:
    env = {
        "DOOR_TOKEN": "t", "PRUNE_BASE_URL": "https://api.withprune.com/", "PRUNE_API_KEY": "k",
        "DOOR_MODELS": "a, b,", "DOOR_RUN_ID": "r", "DOOR_RUN_BUDGET_USD": "0.05",
        "DOOR_MAX_CALLS": "20", "DOOR_CUSTOMER_ID": "team-x", "DOOR_FEATURE_ID": "final_run",
        "DOOR_LOG": str(tmp_path / "l"),
    }  # fmt: skip
    c = DoorConfig.from_env(env)
    assert (c.prune_base_url, c.models, c.max_calls) == (
        "https://api.withprune.com",
        ("a", "b"),
        20,
    )
