"""The runner (section 14.4): read `/work/spec.json`, check the app, write `/work/results.json`.

It runs in its own container on the run's internal network, as the image's non-root user,
with no secrets. As soon as the startup check ends it writes `startup.json`, so the worker can
tell an app that died before answering from one that died mid-test. Only if the app is up
does it run the API tests (`runner/api_tests.py`) on the port that answered. UI tests arrive
in M11. Numbers in `spec.json` are read as `Decimal`, so case values keep their exact text.

Results are written to a temporary name and renamed, so the worker never reads half a file.
A runner that crashes writes no `results.json`, which the worker treats as an infrastructure
error and retries.
"""

import json
import os
import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from runner.api_tests import REQUEST_TIMEOUT_S, run_api_tests
from runner.startup import Probe, check_startup, http_probe, url_for

DEFAULT_HEALTH_PATH = "/"


def run(
    work: Path,
    *,
    probe: Probe = http_probe,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    client: httpx.Client | None = None,
) -> int:
    request = load_request(work)
    startup = check_startup(
        request["base_url"],
        request["candidate_ports"],
        _health_path(request["specs"]),
        timeout_s=request["startup_timeout_s"],
        probe=probe,
        monotonic=monotonic,
        sleep=sleep,
    )
    _write_json(work / "startup.json", startup)

    tests: list[dict[str, Any]] = []
    if startup["status"] == "ok":
        app_url = url_for(request["base_url"], startup["port"], "")
        behaviour = [spec for spec in request["specs"] if spec.get("kind") != "startup"]
        with client or httpx.Client(timeout=REQUEST_TIMEOUT_S, follow_redirects=False) as http:
            tests = run_api_tests(behaviour, request["mode"], app_url, http)
    results = {
        "runner_version": os.environ.get("RUNNER_VERSION", "1"),
        "startup": startup,
        "tests": tests,
    }
    _write_json(work / "results.json", results)
    return 0


def main() -> int:
    return run(Path(os.environ.get("WORK_DIR", "/work")))


def load_request(work: Path) -> dict[str, Any]:
    return load_request_text((work / "spec.json").read_text())


def load_request_text(text: str) -> dict[str, Any]:
    request: dict[str, Any] = json.loads(text, parse_float=Decimal)
    return request


def _health_path(specs: list[dict[str, Any]]) -> str:
    for spec in specs:
        if spec.get("kind") == "startup" and spec.get("steps"):
            return str(spec["steps"][0].get("health_path") or DEFAULT_HEALTH_PATH)
    return DEFAULT_HEALTH_PATH


def _write_json(path: Path, content: object) -> None:
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps(content))
    os.replace(partial, path)
