"""The one client for model calls (section 15).

Every call goes to the AI Credits gateway's chat-completions API (D-037) and asks for exactly one
tool call: the tool's parameters are the output schema, and the reply is parsed into that
Pydantic model or rejected. The request never sets `temperature` or forces the tool, which
Claude Sonnet 5.5 rejects; the system prompt names the tool instead.

A transport sends a request body and returns the reply's JSON: `live` posts to the gateway,
`record` does the same and saves the exchange, `replay` answers from saved exchanges without
touching the network, so fixture runs are free and deterministic. The parser is the same for all
three.

Outcomes: a gateway or network failure is an `InfraError` (the job is retried); a reply that does
not carry a valid tool call is an `LLMOutputError`, whose message can be sent back to the model;
a missing recording is `ReplayMissing`. Every call is logged and, given a database, recorded in
`llm_calls`. The API key appears only in the request's header.
"""

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.db import models
from core.errors import InfraError
from core.hashing import canonical_json
from core.logging import get_logger
from core.settings import Settings

log = get_logger(__name__)

Transport = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
_DETAIL_LIMIT = 2000


class LLMOutputError(Exception):
    """The reply carried no usable tool call. The message says why, in words for the model."""


class ReplayMissing(Exception):
    """Replay mode has no recording for this request."""


@dataclass(frozen=True)
class Tool[T: BaseModel]:
    name: str
    description: str
    schema: type[T]

    def definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.schema.model_json_schema(),
            },
        }


def request_key(body: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(body).encode()).hexdigest()


# --- transports ----------------------------------------------------------------------------------


def live_transport(
    base_url: str, api_key: str, timeout_s: float, *, client: httpx.AsyncClient | None = None
) -> Transport:
    url = f"{base_url.rstrip('/')}/chat/completions"

    async def send(body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            if client is not None:
                response = await client.post(url, json=body, headers=headers, timeout=timeout_s)
            else:
                async with httpx.AsyncClient() as fresh:
                    response = await fresh.post(url, json=body, headers=headers, timeout=timeout_s)
        except httpx.TimeoutException:
            raise InfraError(f"the model gateway did not answer within {timeout_s:g} s") from None
        except httpx.TransportError as exc:
            cause = type(exc).__name__
            raise InfraError(f"the model gateway could not be reached ({cause})") from None
        if response.status_code != 200:
            text = response.text[:300].replace(api_key, "[key]") if api_key else response.text[:300]
            raise InfraError(f"the model gateway answered HTTP {response.status_code}: {text}")
        try:
            data = response.json()
        except ValueError:
            raise InfraError("the model gateway's reply was not JSON") from None
        if not isinstance(data, dict):
            raise InfraError("the model gateway's reply was not a JSON object")
        return data

    return send


def recording_transport(inner: Transport, directory: Path) -> Transport:
    async def send(body: dict[str, Any]) -> dict[str, Any]:
        reply = await inner(body)
        await asyncio.to_thread(_save, directory, body, reply)
        return reply

    return send


def replay_transport(directory: Path) -> Transport:
    async def send(body: dict[str, Any]) -> dict[str, Any]:
        reply = await asyncio.to_thread(_load, directory, body)
        if reply is None:
            raise ReplayMissing(
                f"no recorded reply {request_key(body)} in {directory}; record it with a live "
                "run (LLM_MODE=record)"
            )
        return reply

    return send


def _save(directory: Path, body: dict[str, Any], reply: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    exchange = {"request": body, "response": reply}
    (directory / f"{request_key(body)}.json").write_text(
        json.dumps(exchange, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _load(directory: Path, body: dict[str, Any]) -> dict[str, Any] | None:
    path = directory / f"{request_key(body)}.json"
    if not path.is_file():
        return None
    reply: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))["response"]
    return reply


# --- the client ----------------------------------------------------------------------------------


class LLMClient:
    def __init__(
        self, transport: Transport, *, sessions: async_sessionmaker[AsyncSession] | None = None
    ) -> None:
        self._transport = transport
        self._sessions = sessions

    async def submit[T: BaseModel](
        self,
        *,
        purpose: str,
        model: str,
        system: str,
        user: str,
        tool: Tool[T],
        max_tokens: int,
        job_id: UUID | None = None,
    ) -> T:
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "tools": [tool.definition()],
            "tool_choice": "auto",
        }
        started = time.monotonic()
        usage: dict[str, Any] = {}
        ok = False
        try:
            reply = await self._transport(body)
            reported = reply.get("usage")
            if isinstance(reported, dict):
                usage = reported
            value = _parse(reply, tool)
            ok = True
            return value
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            input_tokens = _int(usage.get("prompt_tokens"))
            output_tokens = _int(usage.get("completion_tokens"))
            log.info(
                "llm_call",
                purpose=purpose,
                model=model,
                ok=ok,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost=usage.get("cost"),
                duration_ms=duration_ms,
            )
            if self._sessions is not None:
                async with self._sessions.begin() as session:
                    session.add(
                        models.LlmCall(
                            job_id=job_id,
                            purpose=purpose,
                            model=model,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            duration_ms=duration_ms,
                            ok=ok,
                        )
                    )


def make_llm_client(
    settings: Settings, sessions: async_sessionmaker[AsyncSession] | None
) -> LLMClient:
    if settings.llm_mode == "replay":
        return LLMClient(replay_transport(settings.llm_replay_dir), sessions=sessions)
    key = settings.llm_api_key.get_secret_value() if settings.llm_api_key is not None else ""
    transport = live_transport(settings.llm_base_url, key, settings.llm_timeout_s)
    if settings.llm_mode == "record":
        transport = recording_transport(transport, settings.llm_replay_dir)
    return LLMClient(transport, sessions=sessions)


# --- parsing -------------------------------------------------------------------------------------


def _parse[T: BaseModel](reply: dict[str, Any], tool: Tool[T]) -> T:
    choices = reply.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise InfraError("the model gateway's reply had no choices")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise InfraError("the model gateway's reply had no message")
    finish = choice.get("finish_reason")
    if finish == "length":
        raise LLMOutputError(f"the reply was cut off at max_tokens before {tool.name} was complete")
    if finish in ("content_filter", "refusal"):
        raise LLMOutputError("the model declined to answer")

    raw = message.get("tool_calls")
    entries = raw if isinstance(raw, list) else []
    calls = [c["function"] for c in entries if isinstance(c, dict) and _is_call(c.get("function"))]
    call = next((c for c in calls if c["name"] == tool.name), None)
    if call is None:
        if calls:
            named = ", ".join(c["name"] for c in calls)
            raise LLMOutputError(f"the reply called {named}, not {tool.name}")
        raise LLMOutputError(f"the reply did not call {tool.name}")
    arguments = call.get("arguments")
    try:
        data = json.loads(arguments) if isinstance(arguments, str) else arguments
    except json.JSONDecodeError as exc:
        raise LLMOutputError(f"the {tool.name} arguments were not valid JSON: {exc}") from None
    try:
        return tool.schema.model_validate(data)
    except ValidationError as exc:
        detail = str(exc)[:_DETAIL_LIMIT]
        raise LLMOutputError(
            f"the {tool.name} arguments did not match its schema: {detail}"
        ) from None


def _is_call(function: Any) -> bool:
    return isinstance(function, dict) and isinstance(function.get("name"), str)


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
