"""The model door: the one way a team's app may reach a model during a run (Prune design, B1).

One door runs per run attempt, on the run's internal network and on a second network with a way
out. It is not a general proxy. It answers three routes:

- `GET /v1/models`: the event's allowed models, answered here.
- `POST /v1/chat/completions` (OpenAI) and `POST /v1/messages` (Anthropic): with this run's
  token, a JSON body under the size cap and a model on the list, forwarded to Prune and to
  nothing else, with the Prune key the app never sees.

Every `X-Prune-*` header the app sent is dropped (a team cannot switch on Prune's cache,
cheaper-model routing, batching or budget downgrade, which would change what its app receives);
the door sets the run's own: its id, budget, step cap, team and feature, and the cache off. It
counts calls itself as a second guard. Each call adds one JSON line to the log the worker
mounted: time, route, model, status, Prune's Shield code, tokens, cost and what Prune applied.
Never the prompt and never the reply.
"""

import asyncio
import contextlib
import hmac
import json
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

ROUTES = ("/v1/chat/completions", "/v1/messages")
# Request headers passed on to Prune; everything else the app sent is dropped.
PASSED = ("content-type", "accept", "anthropic-version", "anthropic-beta")
# Prune's own response headers worth logging.
LOGGED_HEADERS = {
    "x-prune-shield-code": "shield_code",
    "x-prune-cache-hit": "cache_hit",
    "x-prune-route-applied": "route_applied",
    "x-prune-routed-model": "routed_model",
}


@dataclass(frozen=True)
class DoorConfig:
    token: str
    prune_base_url: str
    prune_api_key: str
    models: tuple[str, ...]
    run_id: str
    run_budget_usd: str
    max_calls: int
    customer_id: str
    feature_id: str
    log_path: Path
    max_body_bytes: int = 262144
    max_concurrent: int = 4
    timeout_s: float = 60.0
    egress_hosts: tuple[str, ...] = ()  # the app's own outside calls (door/egress.py)
    max_egress: int = 200

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "DoorConfig":
        e = dict(os.environ if env is None else env)
        return cls(
            token=e["DOOR_TOKEN"],
            prune_base_url=e["PRUNE_BASE_URL"].rstrip("/"),
            prune_api_key=e["PRUNE_API_KEY"],
            models=tuple(m.strip() for m in e["DOOR_MODELS"].split(",") if m.strip()),
            run_id=e["DOOR_RUN_ID"],
            run_budget_usd=e["DOOR_RUN_BUDGET_USD"],
            max_calls=int(e["DOOR_MAX_CALLS"]),
            customer_id=e["DOOR_CUSTOMER_ID"],
            feature_id=e["DOOR_FEATURE_ID"],
            log_path=Path(e.get("DOOR_LOG", "/door/log.jsonl")),
            max_body_bytes=int(e.get("DOOR_MAX_BODY_BYTES", "262144")),
            max_concurrent=int(e.get("DOOR_MAX_CONCURRENT", "4")),
            timeout_s=float(e.get("DOOR_TIMEOUT_S", "60")),
            egress_hosts=tuple(
                h.strip() for h in e.get("DOOR_EGRESS_HOSTS", "").split(",") if h.strip()
            ),
            max_egress=int(e.get("DOOR_MAX_EGRESS", "200")),
        )


def _refuse(status: int, reason: str, code: str) -> JSONResponse:
    """A refusal the team can read in its app's log, in both SDKs' error shapes."""
    body = {"type": "error", "error": {"type": "model_door", "code": code, "message": reason}}
    return JSONResponse(body, status_code=status, headers={"X-Door-Refusal": code})


class Door:
    def __init__(self, config: DoorConfig, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self.calls = 0
        self._lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(config.max_concurrent)
        self._client = httpx.AsyncClient(
            base_url=config.prune_base_url,
            transport=transport,
            timeout=httpx.Timeout(config.timeout_s, connect=10.0),
            follow_redirects=False,
            trust_env=False,  # no proxy settings from the environment: Prune and nothing else
        )

    def log(self, **fields: Any) -> None:
        line = {"at": round(time.time(), 3), **fields}
        with self.config.log_path.open("a", encoding="utf-8") as out:
            out.write(json.dumps(line, sort_keys=True) + "\n")

    def _authorised(self, request: Request) -> bool:
        given = request.headers.get("x-api-key", "")
        bearer = request.headers.get("authorization", "")
        if bearer.lower().startswith("bearer "):
            given = given or bearer[7:].strip()
        return hmac.compare_digest(given.encode(), self.config.token.encode())

    def _headers(self, request: Request) -> dict[str, str]:
        c = self.config
        out = {k: v for k, v in request.headers.items() if k.lower() in PASSED}
        out.update(
            {
                "Authorization": f"Bearer {c.prune_api_key}",
                "x-api-key": c.prune_api_key,
                "X-Prune-Run-Id": c.run_id,
                "X-Prune-Run-Budget-Usd": c.run_budget_usd,
                "X-Prune-Max-Steps": str(c.max_calls),
                "X-Prune-Customer-Id": c.customer_id,
                "X-Prune-Feature-Id": c.feature_id,
                "X-Prune-Cache": "off",
            }
        )
        return out

    async def models(self, request: Request) -> Response:
        if not self._authorised(request):
            return _refuse(
                401, "This run's model door needs the key HackProof gave the app.", "token"
            )
        data = [
            {"id": m, "object": "model", "owned_by": "hackproof-door"} for m in self.config.models
        ]
        return JSONResponse({"object": "list", "data": data})

    async def forward(self, request: Request) -> Response:
        route = request.url.path
        if not self._authorised(request):
            self.log(route=route, status=401, refused="token")
            return _refuse(
                401, "This run's model door needs the key HackProof gave the app.", "token"
            )
        raw = b""
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > self.config.max_body_bytes:
                self.log(route=route, status=413, refused="body_too_large")
                limit = self.config.max_body_bytes // 1024
                return _refuse(413, f"A model request may be at most {limit} KB.", "body_too_large")
        try:
            body = json.loads(raw)
            model = body.get("model") if isinstance(body, dict) else None
        except ValueError:
            body, model = None, None
        if not isinstance(body, dict) or not isinstance(model, str):
            self.log(route=route, status=400, refused="bad_body")
            return _refuse(400, "A model request is a JSON object with a model.", "bad_body")
        if model not in self.config.models:
            self.log(route=route, model=model, status=400, refused="model_not_allowed")
            allowed = ", ".join(self.config.models)
            return _refuse(
                400,
                f"The model {model} is not allowed in this event. Allowed: {allowed}.",
                "model_not_allowed",
            )
        async with self._lock:
            if self.calls >= self.config.max_calls:
                self.log(route=route, model=model, status=429, refused="call_cap")
                return _refuse(429, "This run's model calls are used up.", "call_cap")
            self.calls += 1
        stream = bool(body.get("stream"))
        async with self._slots:
            try:
                upstream = self._client.build_request(
                    "POST", route, content=raw, headers=self._headers(request)
                )
                reply = await self._client.send(upstream, stream=True)
            except httpx.HTTPError as error:
                self.log(route=route, model=model, status=502, unreachable=type(error).__name__)
                return _refuse(502, "The model service could not be reached.", "unreachable")
            if stream and reply.status_code == 200:
                return StreamingResponse(
                    self._relay(reply, route, model),
                    status_code=200,
                    media_type=reply.headers.get("content-type", "text/event-stream"),
                )
            content = await reply.aread()
            await reply.aclose()
        reshaped: dict[str, str] = {}
        if route == "/v1/chat/completions" and reply.status_code == 200:
            openai = _openai_from_gemini(content)
            if openai is not None:
                content, reshaped = openai, {"reshaped": "gemini"}
        self.log(
            route=route, model=model, status=reply.status_code,
            **_summary(reply, content), **reshaped,
        )  # fmt: skip
        headers = {k: v for k, v in reply.headers.items() if k.lower().startswith("x-prune-")}
        return Response(
            content,
            status_code=reply.status_code,
            media_type=reply.headers.get("content-type", "application/json"),
            headers=headers,
        )

    async def _relay(self, reply: httpx.Response, route: str, model: str) -> Any:
        sent = 0
        try:
            async for chunk in reply.aiter_raw():
                sent += len(chunk)
                yield chunk
        finally:
            await reply.aclose()
            self.log(route=route, model=model, status=200, stream=True, bytes=sent,
                     **_summary(reply, b""))  # fmt: skip

    async def refuse_other(self, request: Request) -> Response:
        self.log(route=request.url.path, method=request.method, status=404, refused="route")
        return _refuse(
            404,
            "The model door answers only POST /v1/chat/completions, POST /v1/messages "
            "and GET /v1/models.",
            "route",
        )


# Gemini's finish reasons in OpenAI's words; anything else is a plain stop.
_FINISH = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    **dict.fromkeys(
        ("SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY"),
        "content_filter",
    ),
}


def _openai_from_gemini(content: bytes) -> bytes | None:
    """Prune answers a Gemini model on /v1/chat/completions with Gemini's own body (seen live,
    2026-11-15), which a stock OpenAI SDK cannot read. The door promises the OpenAI shape on that
    route, so it rewrites such a body: text parts joined (thought parts left out), function calls
    as tool calls, the finish reason and usage in OpenAI's words, `prune_metadata` kept. Any
    other body is not Gemini's and is returned as None, to be passed on as it came."""
    try:
        data = json.loads(content)
    except ValueError:
        return None
    if (
        not isinstance(data, dict)
        or "choices" in data
        or not isinstance(data.get("candidates"), list)
    ):
        return None
    choices = []
    for index, candidate in enumerate(data["candidates"]):
        candidate = candidate if isinstance(candidate, dict) else {}
        parts = (candidate.get("content") or {}).get("parts") or []
        texts: list[str] = []
        calls: list[dict[str, Any]] = []
        for part in parts:
            if not isinstance(part, dict) or part.get("thought"):
                continue
            if isinstance(part.get("text"), str):
                texts.append(part["text"])
            call = part.get("functionCall")
            if isinstance(call, dict):
                calls.append({
                    "id": f"call_{data.get('responseId', 'door')}_{index}_{len(calls)}",
                    "type": "function",
                    "function": {"name": call.get("name", ""),
                                 "arguments": json.dumps(call.get("args") or {})},
                })  # fmt: skip
        message: dict[str, Any] = {"role": "assistant", "content": "".join(texts) or None}
        finish = _FINISH.get(str(candidate.get("finishReason", "STOP")), "stop")
        if calls:
            message["tool_calls"], finish = calls, "tool_calls"
        choices.append(
            {"index": candidate.get("index", index), "message": message, "finish_reason": finish}
        )
    meta = data.get("usageMetadata") or {}
    prompt = int(meta.get("promptTokenCount") or 0)
    completion = int(meta.get("candidatesTokenCount") or 0) + int(
        meta.get("thoughtsTokenCount") or 0
    )
    out: dict[str, Any] = {
        "id": f"chatcmpl-{data.get('responseId', 'door')}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": data.get("modelVersion", ""),
        "choices": choices,
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": int(meta.get("totalTokenCount") or prompt + completion),
        },
    }
    if "prune_metadata" in data:
        out["prune_metadata"] = data["prune_metadata"]
    return json.dumps(out).encode()


def _summary(reply: httpx.Response, content: bytes) -> dict[str, Any]:
    """What one call used, from Prune's headers and its `prune_metadata`: never the text."""
    out: dict[str, Any] = {
        name: reply.headers[header]
        for header, name in LOGGED_HEADERS.items()
        if header in reply.headers
    }
    try:
        data = json.loads(content) if content else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        return out
    usage = data.get("usage") or {}
    if isinstance(usage, dict):
        for key in ("prompt_tokens", "completion_tokens", "input_tokens", "output_tokens"):
            if isinstance(usage.get(key), int):
                out[key] = usage[key]
    meta = data.get("prune_metadata") or {}
    if isinstance(meta, dict):
        for key in ("cost_with_prune_usd", "cache_hit", "optimizations_applied", "model"):
            if key in meta:
                out[f"prune_{key}"] = meta[key]
    error = data.get("error")
    if isinstance(error, dict) and isinstance(error.get("code"), str):
        out["error_code"] = error["code"]
        # Prune's own setup problem (no provider key in its vault) is ours, not the team's.
        if "vault" in str(error.get("message", "")).lower():
            out["error_code"] = "missing_provider_key"
    return out


def make_app(
    config: DoorConfig,
    transport: httpx.AsyncBaseTransport | None = None,
    *,
    ready_path: Path | None = None,
) -> Starlette:
    door = Door(config, transport)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        # The worker starts the app only once this file exists: the door is listening.
        if ready_path is not None:
            ready_path.write_text("ready\n")  # noqa: ASYNC240 - once, before any request
        yield
        await door._client.aclose()

    routes = [
        Route("/v1/models", door.models, methods=["GET"]),
        *(Route(path, door.forward, methods=["POST"]) for path in ROUTES),
        Route("/{rest:path}", door.refuse_other, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.door = door
    return app
