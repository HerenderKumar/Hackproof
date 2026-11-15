"""UI tests (section 14.2, M11 design parts 3 and 4): carry out each case's steps in a browser.

Chromium starts once per run, and only when the run carries a UI spec. Each case gets a fresh
context, so cases share no cookies, storage or open pages. Steps are a fixed table of actions,
each one Playwright call: no model and no generated code (D-010). The browser reaches the app by
its IP address, since `app` is a top-level domain Chromium forces to HTTPS (D-058).

Three things keep a case repeatable and contained: every request to anything but the app is
aborted and listed; a pop-up is closed and an alert accepted, both noted; each step has 10 s and
each case 45 s.

How a step ended is decided by counting what its locator matches after the step fails, not by
reading Playwright's error text: nothing matched is `locator_not_found` and several is
`locator_ambiguous` (both `unverified`); one element that does not hold what a check expected is
`fail`; one element that could not be used in time is `timeout` (`unverified`).

Each case leaves `/work/ui/<spec_id>/<n>/` with `video.webm` (captioned with the step being
carried out), `trace.zip` and `poster.jpg`. The worker decides which of those files it keeps.
"""

import contextlib
import os
import socket
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from core.specs import ACTING, Case, Locator, Mode, UiSpecShape, UiStep
from core.templating import TemplateError, resolve_text
from core.ui_words import step_words

STEP_TIMEOUT_S = 10.0
CASE_TIMEOUT_S = 45.0
VIEWPORT = {"width": 1280, "height": 720}
OUTLINE_LIMIT = 8_192
TEXT_LIMIT = 500
NOTES_LIMIT = 20
UI_DIR = "ui"

Launch = Callable[[], Any]  # returns a started Playwright `Browser`


class _Stop(Exception):
    """A step ended the case. `status` is `fail` or `error`, `kind` the error type."""

    def __init__(
        self, status: str, kind: str | None, message: str, check: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.status, self.kind, self.message, self.check = status, kind, message, check


def has_ui(specs: list[dict[str, Any]]) -> bool:
    return any(spec.get("kind") == "ui" for spec in specs)


def browser_origin(app_url: str) -> str:
    """`app_url` with its host replaced by the address it resolves to (D-058)."""
    parts = urllib.parse.urlsplit(app_url)
    address = socket.gethostbyname(parts.hostname or "")
    netloc = f"{address}:{parts.port}" if parts.port else address
    return urllib.parse.urlunsplit((parts.scheme, netloc, "", "", "")).rstrip("/")


def run_ui_tests(
    specs: list[dict[str, Any]],
    mode: Mode,
    origin: str,
    work: Path,
    launch: Launch,
) -> list[dict[str, Any]]:
    """One result per UI spec and case, in order. `origin` is the app as the browser reaches it."""
    ui_specs = [spec for spec in specs if spec.get("kind") == "ui"]
    if not ui_specs:
        return []
    browser, problem = _start(launch)
    entries: list[dict[str, Any]] = []
    try:
        for raw in ui_specs:
            spec_id = str(raw.get("spec_id", ""))
            try:
                spec = UiSpecShape.model_validate(raw)
            except ValidationError as exc:
                entries.append(_error_entry(spec_id, None, "spec", f"the spec is malformed: {exc}"))
                continue
            cases = spec.final_cases if mode == "final" else spec.practice_cases
            for number, case in enumerate(cases, start=1):
                if browser is None:
                    entries.append(_error_entry(spec_id, case.id, "runner", problem))
                    continue
                folder = work / UI_DIR / _safe(spec_id) / str(number)
                entries.append(run_case(browser, spec, spec_id, case, origin, folder))
    finally:
        if browser is not None:
            with contextlib.suppress(Exception):
                browser.close()
    return entries


def _start(launch: Launch) -> tuple[Any, str]:
    """The browser, after one retry; or None and why it would not start."""
    problem = ""
    for _ in range(2):
        try:
            return launch(), ""
        except Exception as exc:
            problem = f"the browser would not start: {type(exc).__name__}: {str(exc)[:300]}"
    return None, problem


def run_case(
    browser: Any, spec: UiSpecShape, spec_id: str, case: Case, origin: str, folder: Path
) -> dict[str, Any]:
    started = time.monotonic()
    deadline = started + CASE_TIMEOUT_S
    folder.mkdir(parents=True, exist_ok=True)
    notes: dict[str, list[Any]] = {
        "refused": [], "console_errors": [], "page_errors": [], "dialogs": [], "popups": [],
    }  # fmt: skip
    context = browser.new_context(
        viewport=VIEWPORT,
        locale="en-US",
        timezone_id="UTC",
        color_scheme="light",
        reduced_motion="reduce",
        service_workers="block",
        accept_downloads=False,
    )
    context.route("**/*", _only(origin, notes["refused"]))
    context.tracing.start(
        title=f"{spec.claim_id} case {case.id}",
        screenshots=True,
        snapshots=True,
        screen_snapshots=True,
        aria_snapshots=True,
    )
    page = context.new_page()
    _listen(page, notes)
    video = folder / "video.webm"
    recording = _start_recording(page, video, f"{spec.claim_id} · case {case.id}", case)

    values = {"case": {"input": case.input, "expected": case.expected}, "saved": {}}
    steps: list[dict[str, Any]] = []
    assertions: list[dict[str, Any]] = []
    status, error = "pass", None
    for step in spec.steps:
        if status != "pass":
            steps.append({"id": step.id, "action": step.action, "words": step_words(step),
                          "status": "skipped"})  # fmt: skip
            continue
        record: dict[str, Any] = {"id": step.id, "action": step.action}
        steps.append(record)
        step_started = time.monotonic()
        try:
            filled = _fill(step, values)
            record["words"] = step_words(filled)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _Stop("error", "timeout", f"the case took more than {CASE_TIMEOUT_S:g} s")
            timeout_ms = min(STEP_TIMEOUT_S, remaining) * 1000
            context.tracing.group(record["words"])
            try:
                check = _carry_out(page, filled, origin, timeout_ms)
            finally:
                context.tracing.group_end()
            if check is not None:
                assertions.append(check | {"step": step.id})
            record["status"] = "pass"
        except _Stop as stop:
            status = stop.status
            error = {"type": stop.kind, "message": stop.message} if stop.kind else None
            record["status"] = stop.status
            record["message"] = stop.message
            if stop.check is not None:
                record["expected"], record["actual"] = stop.check["expected"], stop.check["actual"]
                assertions.append(stop.check | {"step": step.id})
        except Exception as exc:  # the runner's own fault, never the app's
            status, error = "error", {"type": "runner", "message": f"{type(exc).__name__}: {exc}"}
            record["status"] = "error"
            record["message"] = error["message"][:TEXT_LIMIT]
        record.setdefault("words", step_words(step))
        record["duration_ms"] = round((time.monotonic() - step_started) * 1000)

    page_details = _details(page, notes) if status != "pass" else _brief_notes(notes)
    files = _finish(context, page, folder, recording)
    return {
        "spec_id": spec_id,
        "case_id": case.id,
        "kind": "ui",
        "status": status,
        "error": error,
        "assertions": assertions,
        "steps": steps,
        "request": None,
        "response": None,
        "page": page_details,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "video_path": files.get("video"),
        "trace_path": files.get("trace"),
    }


# --- one step ------------------------------------------------------------------------------------


def _fill(step: UiStep, values: dict[str, Any]) -> UiStep:
    """The step with its placeholders filled as text, checked again (a path stays on the app)."""
    data = step.model_dump(exclude_none=True)

    def fill(value: Any) -> Any:
        if isinstance(value, str):
            return resolve_text(value, values)
        if isinstance(value, dict):
            return {key: fill(item) for key, item in value.items()}
        return value

    try:
        return UiStep.model_validate(fill(data))
    except TemplateError as exc:
        raise _Stop("error", "spec", str(exc)) from None
    except ValidationError as exc:
        raise _Stop("error", "spec", f"the step is malformed once filled: {exc}") from None


def find(page: Any, locator: Locator) -> Any:
    """The Playwright locator for a spec's locator."""
    exact = {"exact": True} if locator.exact else {}
    if locator.role is not None:
        named = {"name": locator.name} if locator.name is not None else {}
        return page.get_by_role(locator.role, **named, **exact)
    if locator.label is not None:
        return page.get_by_label(locator.label, **exact)
    if locator.placeholder is not None:
        return page.get_by_placeholder(locator.placeholder, **exact)
    if locator.text is not None:
        return page.get_by_text(locator.text, **exact)
    return page.get_by_test_id(locator.test_id or "")


def _carry_out(page: Any, step: UiStep, origin: str, timeout_ms: float) -> dict[str, Any] | None:
    """Do one step. Returns the check it made, if any; raises `_Stop` when it does not hold."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import expect

    words = step_words(step)
    target = find(page, step.locator).describe(words) if step.locator is not None else None
    try:
        match step.action:
            case "goto":
                response = page.goto(origin + (step.path or "/"), wait_until="load",
                                     timeout=timeout_ms)  # fmt: skip
                if response is not None and response.status >= 400:
                    raise _Stop("fail", None, f"{step.path} answered HTTP {response.status}")
                return None
            case "fill":
                target.fill(step.value or "", timeout=timeout_ms)
            case "click":
                target.click(timeout=timeout_ms)
            case "select":
                target.select_option(step.value, timeout=timeout_ms)
            case "press":
                if target is not None:
                    target.press(_key(step.key or ""), timeout=timeout_ms)
                else:
                    page.keyboard.press(_key(step.key or ""))
            case "wait_for":
                target.wait_for(state="visible", timeout=timeout_ms)
            case "expect_visible":
                expect(target).to_be_visible(timeout=timeout_ms)
                return _check(step, "visible", "visible", True)
            case "expect_text":
                expect(target).to_contain_text(step.text or "", timeout=timeout_ms)
                return _check(step, step.text, _text_of(target), True)
            case "expect_value":
                expect(target).to_have_value(step.value or "", timeout=timeout_ms)
                return _check(step, step.value, step.value, True)
            case "expect_url":
                expect(page).to_have_url(origin + (step.path or "/"), timeout=timeout_ms)
                return _check(step, step.path, step.path, True)
        return None
    except _Stop:
        raise
    except (AssertionError, PlaywrightTimeout, PlaywrightError) as exc:
        raise _why(page, step, target, origin, exc) from None


def _why(page: Any, step: UiStep, target: Any, origin: str, exc: BaseException) -> _Stop:
    """What a failed step means, from what the page holds now (part 4's table)."""
    message = str(exc).split("\n", 1)[0][:TEXT_LIMIT]
    words = step_words(step)
    if step.action == "goto":
        if "ERR_CONNECTION_REFUSED" in message or "ERR_CONNECTION_RESET" in message:
            return _Stop("error", "connection_refused", f"the app stopped answering: {message}")
        if "Timeout" in type(exc).__name__ or "Timeout" in message:
            return _Stop("error", "timeout", f"{step.path} did not load in time")
        return _Stop("error", "runner", f"{step.path} could not be opened: {message}")
    if step.action == "expect_url":
        actual = _path_of(page.url, origin)
        check = _check(step, step.path, actual, False)
        return _Stop("fail", None, f"the page's address is {actual}, not {step.path}", check)
    if target is None:  # a key press on the page itself
        return _Stop("error", "timeout", f"{words}: {message}")
    try:
        count = target.count()
    except Exception:
        return _Stop("error", "runner", f"{words}: {message}")
    if count == 0:
        return _Stop("error", "locator_not_found", f"nothing on the page matched: {words}")
    if count > 1:
        return _Stop(
            "error", "locator_ambiguous", f"{count} elements matched, so the test cannot tell "
            f"which one is meant: {words}"
        )  # fmt: skip
    if step.action in ACTING or step.action == "wait_for":
        return _Stop(
            "error", "timeout", f"the element was there but could not be used in time "
            f"(disabled, hidden or covered): {words}"
        )  # fmt: skip
    expected, actual = _expected_actual(step, target)
    check = _check(step, expected, actual, False)
    return _Stop("fail", None, f"{words}: it holds {_shown(actual)}", check)


def _expected_actual(step: UiStep, target: Any) -> tuple[Any, Any]:
    if step.action == "expect_visible":
        return "visible", "hidden"
    if step.action == "expect_value":
        try:
            return step.value, target.input_value(timeout=1000)
        except Exception:
            return step.value, None
    return step.text, _text_of(target)


def _text_of(target: Any) -> str | None:
    try:
        return str(target.inner_text(timeout=1000))[:TEXT_LIMIT]
    except Exception:
        return None


def _check(step: UiStep, expected: Any, actual: Any, passed: bool) -> dict[str, Any]:
    """A check in the shape API assertions have, so existing displays read it."""
    return {
        "op": step.action,
        "path": step_words(step),
        "expected": expected,
        "actual": actual,
        "passed": passed,
        "note": None,
    }


def _shown(value: Any) -> str:
    if value is None:
        return "nothing that could be read"
    if value == "":
        return "nothing (it is empty)"
    return f"“{value}”"


def _key(key: str) -> str:
    return " " if key == "Space" else key


def _path_of(url: str, origin: str) -> str:
    return url[len(origin) :] or "/" if url.startswith(origin) else url


# --- the context ---------------------------------------------------------------------------------


def _only(origin: str, refused: list[str]) -> Callable[[Any], None]:
    def route(handle: Any) -> None:
        url = str(handle.request.url)
        if url == origin or url.startswith(origin + "/"):
            handle.continue_()
            return
        if len(refused) < NOTES_LIMIT and url not in refused:
            refused.append(url[:200])
        handle.abort("blockedbyclient")

    return route


def _listen(page: Any, notes: dict[str, list[Any]]) -> None:
    def add(kind: str, item: Any) -> None:
        if len(notes[kind]) < NOTES_LIMIT:
            notes[kind].append(item)

    def on_console(message: Any) -> None:
        if message.type == "error":
            location = message.location or {}
            text = str(message.text)[:TEXT_LIMIT]
            line = location.get("lineNumber")
            add("console_errors", {"text": text, "url": location.get("url"), "line": line})

    def on_dialog(dialog: Any) -> None:
        add("dialogs", {"type": dialog.type, "message": str(dialog.message)[:TEXT_LIMIT]})
        with contextlib.suppress(Exception):
            dialog.accept()

    def on_popup(popup: Any) -> None:
        add("popups", str(popup.url)[:200])
        with contextlib.suppress(Exception):
            popup.close()

    page.on("console", on_console)
    page.on("pageerror", lambda error: add("page_errors", str(error)[:TEXT_LIMIT]))
    page.on("dialog", on_dialog)
    page.on("popup", on_popup)


def _start_recording(page: Any, video: Path, title: str, case: Case) -> bool:
    try:
        page.screencast.start(path=str(video), size=VIEWPORT)
        page.screencast.show_actions(position="top-right")
        inputs = ", ".join(f"{key}: {value}" for key, value in case.input.items())
        page.screencast.show_chapter(title, description=inputs[:200], duration=1200)
        return True
    except Exception:
        return False


def _details(page: Any, notes: dict[str, list[Any]]) -> dict[str, Any]:
    """What the page had when the case did not pass, for the team and the judge."""
    try:
        outline = str(page.aria_snapshot(timeout=2000))
    except Exception:
        outline = ""
    return {"outline": outline[:OUTLINE_LIMIT], **notes}


def _brief_notes(notes: dict[str, list[Any]]) -> dict[str, Any] | None:
    kept = {kind: items for kind, items in notes.items() if items}
    return kept or None


def _finish(context: Any, page: Any, folder: Path, recording: bool) -> dict[str, str]:
    """Write the still image, the video and the trace. Returns the files written, by kind."""
    written: dict[str, str] = {}
    work = folder.parent.parent.parent
    with contextlib.suppress(Exception):
        page.screenshot(path=str(folder / "poster.jpg"), type="jpeg", quality=70, timeout=5000)
    if recording:
        with contextlib.suppress(Exception):
            page.screencast.stop()
    with contextlib.suppress(Exception):
        context.tracing.stop(path=str(folder / "trace.zip"))
    with contextlib.suppress(Exception):
        context.close()
    for kind, name in (("video", "video.webm"), ("trace", "trace.zip"), ("poster", "poster.jpg")):
        if (folder / name).is_file():
            written[kind] = str((folder / name).relative_to(work))
    return written


def _safe(spec_id: str) -> str:
    """A folder name from a spec ID (a UUID the worker made; anything else is flattened)."""
    return "".join(ch for ch in spec_id if ch.isalnum() or ch == "-")[:64] or "spec"


def _error_entry(spec_id: str, case_id: str | None, kind: str, message: str) -> dict[str, Any]:
    return {
        "spec_id": spec_id,
        "case_id": case_id,
        "kind": "ui",
        "status": "error",
        "error": {"type": kind, "message": message},
        "assertions": [],
        "steps": [],
        "request": None,
        "response": None,
        "page": None,
        "duration_ms": 0,
        "video_path": None,
        "trace_path": None,
    }


UiRunner = Callable[[list[dict[str, Any]], Mode, str, Path], list[dict[str, Any]]]


def launcher(playwright: Any) -> Launch:
    """Start Chromium with its own sandbox unless `RUNNER_BROWSER_SANDBOX=false` (D-058)."""
    sandbox = os.environ.get("RUNNER_BROWSER_SANDBOX", "true").lower() != "false"
    return lambda: playwright.chromium.launch(chromium_sandbox=sandbox)


def playwright_ui_runner(
    specs: list[dict[str, Any]], mode: Mode, app_url: str, work: Path
) -> list[dict[str, Any]]:
    """`run_ui_tests` with a real Playwright, reaching the app at `app_url` by its address.
    Playwright is imported here, so API-only runs never load it."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        return run_ui_tests(specs, mode, browser_origin(app_url), work, launcher(playwright))
