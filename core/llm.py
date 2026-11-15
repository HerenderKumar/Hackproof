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
import re
import time
from collections.abc import Awaitable, Callable, Sequence
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


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # as the model sent it: JSON text, not yet parsed


@dataclass(frozen=True)
class StepReply:
    """One turn of a conversation: the assistant message to append, and the calls it makes."""

    message: dict[str, Any]
    calls: tuple[ToolCall, ...]
    finish: str | None
    prompt_tokens: int
    completion_tokens: int


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def canonical_ids(body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """The body with every UUID replaced by a numbered placeholder, and placeholder -> real ID.

    A request can name rows made fresh on every run (a brief names its test results), so the
    recording is keyed and stored with placeholders, numbered in order of first appearance.
    """
    real_to_placeholder: dict[str, str] = {}

    def swap(match: re.Match[str]) -> str:
        real = match.group(0)
        if real not in real_to_placeholder:
            real_to_placeholder[real] = (
                f"00000000-0000-4000-8000-{len(real_to_placeholder) + 1:012d}"
            )
        return real_to_placeholder[real]

    text = _UUID.sub(swap, canonical_json(body))
    canonical: dict[str, Any] = json.loads(text)
    return canonical, {placeholder: real for real, placeholder in real_to_placeholder.items()}


def _substitute(data: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
    if not mapping:
        return data
    text = _UUID.sub(lambda m: mapping.get(m.group(0), m.group(0)), json.dumps(data))
    swapped: dict[str, Any] = json.loads(text)
    return swapped


def request_key(body: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(canonical_ids(body)[0]).encode()).hexdigest()


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
            # Mask before cutting: a key cut in two by the limit would leave its first part.
            text = (response.text.replace(api_key, "[key]") if api_key else response.text)[:300]
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
    canonical, placeholders = canonical_ids(body)
    to_placeholder = {real: placeholder for placeholder, real in placeholders.items()}
    directory.mkdir(parents=True, exist_ok=True)
    exchange = {"request": canonical, "response": _substitute(reply, to_placeholder)}
    (directory / f"{request_key(body)}.json").write_text(
        json.dumps(exchange, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _load(directory: Path, body: dict[str, Any]) -> dict[str, Any] | None:
    """The reply recorded for this request, from the directory or one folder inside it."""
    name = f"{request_key(body)}.json"
    folders = [directory]
    if directory.is_dir():
        folders += sorted(p for p in directory.iterdir() if p.is_dir())
    for folder in folders:
        path = folder / name
        if path.is_file():
            reply: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))["response"]
            return _substitute(reply, canonical_ids(body)[1])
    return None


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
            usage = _usage(reply)
            value = _parse(reply, tool)
            ok = True
            return value
        except ReplayMissing as exc:
            raise ReplayMissing(f"{purpose}: {exc}") from None
        finally:
            await self._record(purpose, model, ok, usage, started, job_id)

    async def step(
        self,
        *,
        purpose: str,
        model: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[Tool[Any]],
        max_tokens: int,
        job_id: UUID | None = None,
    ) -> StepReply:
        """One request of a conversation with several tools (the audit's loop, section 15.2).

        The reply may call any number of the tools, or none; the caller runs them and sends their
        results back in the next step's messages.
        """
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": list(messages),
            "tools": [tool.definition() for tool in tools],
            "tool_choice": "auto",
        }
        started = time.monotonic()
        usage: dict[str, Any] = {}
        ok = False
        try:
            reply = await self._transport(body)
            usage = _usage(reply)
            step = _step_reply(reply, usage)
            ok = True
            return step
        except ReplayMissing as exc:
            raise ReplayMissing(f"{purpose}: {exc}") from None
        finally:
            await self._record(purpose, model, ok, usage, started, job_id)

    async def _record(
        self,
        purpose: str,
        model: str,
        ok: bool,
        usage: dict[str, Any],
        started: float,
        job_id: UUID | None,
    ) -> None:
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


def make_transport(settings: Settings) -> Transport:
    """The transport `LLM_MODE` asks for. Replay needs no key."""
    if settings.llm_mode == "replay":
        return replay_transport(settings.llm_replay_dir)
    key = settings.llm_api_key.get_secret_value() if settings.llm_api_key is not None else ""
    transport = live_transport(settings.llm_base_url, key, settings.llm_timeout_s)
    if settings.llm_mode == "record":
        transport = recording_transport(transport, settings.llm_replay_dir)
    return transport


def make_llm_client(
    settings: Settings, sessions: async_sessionmaker[AsyncSession] | None
) -> LLMClient:
    return LLMClient(make_transport(settings), sessions=sessions)


# --- parsing -------------------------------------------------------------------------------------


def parse_arguments[T: BaseModel](call: ToolCall, tool: Tool[T]) -> T:
    """A call's arguments as the tool's model. Raises `LLMOutputError`, in words for the model."""
    try:
        data = json.loads(call.arguments)
    except json.JSONDecodeError as exc:
        raise LLMOutputError(f"the {tool.name} arguments were not valid JSON: {exc}") from None
    return _validate(data, tool)


def _usage(reply: dict[str, Any]) -> dict[str, Any]:
    reported = reply.get("usage")
    return reported if isinstance(reported, dict) else {}


def _choice(reply: dict[str, Any]) -> tuple[dict[str, Any], Any]:
    choices = reply.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise InfraError("the model gateway's reply had no choices")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise InfraError("the model gateway's reply had no message")
    return message, choice.get("finish_reason")


def _step_reply(reply: dict[str, Any], usage: dict[str, Any]) -> StepReply:
    message, finish = _choice(reply)
    raw = message.get("tool_calls")
    entries = raw if isinstance(raw, list) else []
    calls: list[ToolCall] = []
    for n, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict) or not _is_call(entry.get("function")):
            continue
        function = entry["function"]
        arguments = function.get("arguments")
        text = arguments if isinstance(arguments, str) else json.dumps(arguments)
        call_id = entry.get("id")
        calls.append(ToolCall(call_id if isinstance(call_id, str) else f"call_{n}",
                              function["name"], text))  # fmt: skip
    if not calls and finish == "length":
        raise LLMOutputError("the reply was cut off at max_tokens before it called a tool")
    if not calls and finish in ("content_filter", "refusal"):
        raise LLMOutputError("the model declined to answer")
    content = message.get("content")
    assistant: dict[str, Any] = {
        "role": "assistant",
        "content": content if isinstance(content, str) else None,
    }
    if calls:
        assistant["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
            for c in calls
        ]
    return StepReply(
        message=assistant,
        calls=tuple(calls),
        finish=finish if isinstance(finish, str) else None,
        prompt_tokens=_int(usage.get("prompt_tokens")) or 0,
        completion_tokens=_int(usage.get("completion_tokens")) or 0,
    )


def _validate[T: BaseModel](data: Any, tool: Tool[T]) -> T:
    try:
        return tool.schema.model_validate(data)
    except ValidationError as exc:
        detail = str(exc)[:_DETAIL_LIMIT]
        raise LLMOutputError(
            f"the {tool.name} arguments did not match its schema: {detail}"
        ) from None


def _parse[T: BaseModel](reply: dict[str, Any], tool: Tool[T]) -> T:
    message, finish = _choice(reply)
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
    return _validate(data, tool)


def _is_call(function: Any) -> bool:
    return isinstance(function, dict) and isinstance(function.get("name"), str)


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
