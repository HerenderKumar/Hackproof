"""API tests (section 14.1): run each case's steps against the app and record what happened.

A case runs its steps in order, filling templates from the case and from values saved by
earlier steps, and stops at the first step that fails: later steps usually depend on it, and
the failing step is the evidence. Every request, response and assertion is recorded, so a judge
can check a result instead of trusting it.

What went wrong decides the status. The app's own failures are `fail`: an assertion that did
not hold, a value to save that was not there. An app that stopped answering mid-test is
`connection_refused`, which the worker records as `fail` too. Problems that are not the app's
are errors the worker records as `unverified`: a `timeout`, a broken `spec`, or a crash of the
runner itself (`runner`). UI specs are left to `runner/ui_tests.py`.
"""

import json
import time
import urllib.parse
from decimal import Decimal
from typing import Any

import httpx
from pydantic import ValidationError

from core.assertions import (
    AssertionResult,
    InvalidAssertion,
    Response,
    evaluate,
    find,
    parse_body,
    to_json_value,
)
from core.specs import ApiSpecShape, ApiStep, Assertion, Case, Mode
from core.templating import TemplateError, resolve, resolve_text, text_form

REQUEST_TIMEOUT_S = 10.0
BODY_READ_LIMIT = 1 << 20
STORED_BODY_LIMIT = 16_384


def run_api_tests(
    specs: list[dict[str, Any]], mode: Mode, base_url: str, client: httpx.Client
) -> list[dict[str, Any]]:
    """One result per spec and case, in order. Startup specs are not API tests."""
    entries: list[dict[str, Any]] = []
    for raw in specs:
        spec_id = str(raw.get("spec_id", ""))
        kind = raw.get("kind")
        if kind != "api":  # startup checks are the runner's own; UI tests run in a browser
            continue
        try:
            spec = ApiSpecShape.model_validate(raw)
        except ValidationError as exc:
            entries.append(_error_entry(spec_id, None, "spec", f"the spec is malformed: {exc}"))
            continue
        cases = spec.final_cases if mode == "final" else spec.practice_cases
        to_run: list[Case | None] = [*cases] or [None]  # no cases: run once, empty
        for case in to_run:
            entries.append(run_case(spec, spec_id, case, base_url, client))
    return entries


def run_case(
    spec: ApiSpecShape, spec_id: str, case: Case | None, base_url: str, client: httpx.Client
) -> dict[str, Any]:
    started = time.monotonic()
    context: dict[str, Any] = {
        "case": {"input": case.input if case else {}, "expected": case.expected if case else {}},
        "saved": {},
    }
    steps: list[dict[str, Any]] = []
    assertions: list[dict[str, Any]] = []
    status, error = "pass", None
    evidence: dict[str, Any] = {}  # the failing step, or the last one that ran

    for step in spec.steps:
        if status != "pass":
            steps.append({"id": step.id, "skipped": True})
            continue
        record: dict[str, Any] = {
            "id": step.id,
            "request": None,
            "response": None,
            "assertions": [],
        }
        steps.append(record)
        evidence = record
        try:
            results = _run_step(step, context, base_url, client, record)
        except (TemplateError, InvalidAssertion) as exc:
            status, error = "error", {"type": "spec", "message": str(exc)}
            continue
        except httpx.TimeoutException as exc:
            message = f"no answer within {REQUEST_TIMEOUT_S:g} s: {exc}"
            status, error = "error", {"type": "timeout", "message": message}
            continue
        except httpx.TransportError as exc:
            message = f"the app stopped answering: {exc}"
            status, error = "error", {"type": "connection_refused", "message": message}
            continue
        except Exception as exc:
            status, error = "error", {"type": "runner", "message": f"{type(exc).__name__}: {exc}"}
            continue
        record["assertions"] = [result.to_json() for result in results]
        assertions.extend(record["assertions"])
        if not all(result.passed for result in results):
            status = "fail"

    return {
        "spec_id": spec_id,
        "case_id": case.id if case else None,
        "status": status,
        "error": error,
        "assertions": assertions,
        "steps": steps,
        "request": evidence.get("request"),
        "response": evidence.get("response"),
        "duration_ms": round((time.monotonic() - started) * 1000),
        "video_path": None,
        "trace_path": None,
    }


def _run_step(
    step: ApiStep,
    context: dict[str, Any],
    base_url: str,
    client: httpx.Client,
    record: dict[str, Any],
) -> list[AssertionResult]:
    request = step.request
    path = resolve_text(request.path, context, encode=_quote)
    query = {name: text_form(value) for name, value in resolve(request.query, context).items()}
    headers = {name: text_form(value) for name, value in resolve(request.headers, context).items()}
    payload = _wire(resolve(request.json_body, context))
    record["request"] = {
        "method": request.method,
        "path": path,
        "query": query,
        "headers": dict(headers),  # as the spec set them
        "json": payload,
    }

    content = None
    if request.json_body is not None:
        content = json.dumps(payload).encode()
        if not any(name.lower() == "content-type" for name in headers):
            headers["Content-Type"] = "application/json"
    response, stored = _send(client, request.method, base_url + path, query, headers, content)
    record["response"] = stored

    results = []
    for assertion in step.assertions:
        filled = Assertion.model_validate(resolve(assertion.model_dump(), context))
        results.append(_tagged(evaluate(filled, response), step.id))
    for name, json_path in step.save.items():
        found = find(json_path, response.body)
        if found:
            context["saved"][name] = found[0]
        else:
            note = f"nothing at {json_path} to save as {name}"
            results.append(AssertionResult("save", json_path, name, None, False, note, step.id))
    return results


def _send(
    client: httpx.Client,
    method: str,
    url: str,
    query: dict[str, str],
    headers: dict[str, str],
    content: bytes | None,
) -> tuple[Response, dict[str, Any]]:
    """One request, its body read up to `BODY_READ_LIMIT`. Returns it parsed and as stored."""
    with client.stream(
        method,
        url,
        params=query,
        headers=headers,
        content=content,
        timeout=REQUEST_TIMEOUT_S,
        follow_redirects=False,
    ) as reply:
        data = bytearray()
        cut = False
        for chunk in reply.iter_bytes():
            data += chunk
            if len(data) > BODY_READ_LIMIT:
                del data[BODY_READ_LIMIT:]
                cut = True
                break
        text = bytes(data).decode(reply.charset_encoding or "utf-8", errors="replace")
        names = {name.lower(): value[:1024] for name, value in reply.headers.items()}
    parsed = Response(status=reply.status_code, headers=names, text=text, body=parse_body(text))
    stored = {
        "status": reply.status_code,
        "headers": names,
        "body": text[:STORED_BODY_LIMIT],
        "truncated": cut or len(text) > STORED_BODY_LIMIT,
    }
    return parsed, stored


def _tagged(result: AssertionResult, step_id: str) -> AssertionResult:
    return AssertionResult(
        result.op, result.path, result.expected, result.actual, result.passed, result.note, step_id
    )


def _quote(text: str) -> str:
    return urllib.parse.quote(text, safe="")


def _wire(value: Any) -> Any:
    """A JSON-ready copy: a `Decimal` becomes an int when whole, else a float."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: _wire(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_wire(item) for item in value]
    return to_json_value(value)


def _error_entry(spec_id: str, case_id: str | None, kind: str, message: str) -> dict[str, Any]:
    return {
        "spec_id": spec_id,
        "case_id": case_id,
        "status": "error",
        "error": {"type": kind, "message": message},
        "assertions": [],
        "steps": [],
        "request": None,
        "response": None,
        "duration_ms": 0,
        "video_path": None,
        "trace_path": None,
    }
