"""The runner (section 14.4): read `/work/spec.json`, check the app, write `/work/results.json`.

It runs in its own container on the run's internal network, as the image's non-root user,
with no secrets. As soon as the startup check ends it writes `startup.json`, so the worker can
tell an app that died before answering from one that died mid-test. API tests arrive in M5
and UI tests in M11; until then `tests` is empty.

Results are written to a temporary name and renamed, so the worker never reads half a file.
A runner that crashes writes no `results.json`, which the worker treats as an infrastructure
error and retries.
"""

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from runner.startup import Probe, check_startup, http_probe

DEFAULT_HEALTH_PATH = "/"


def run(
    work: Path,
    *,
    probe: Probe = http_probe,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    request = json.loads((work / "spec.json").read_text())
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
    results = {
        "runner_version": os.environ.get("RUNNER_VERSION", "1"),
        "startup": startup,
        "tests": tests,
    }
    _write_json(work / "results.json", results)
    return 0


def main() -> int:
    return run(Path(os.environ.get("WORK_DIR", "/work")))


def _health_path(specs: list[dict[str, Any]]) -> str:
    for spec in specs:
        if spec.get("kind") == "startup" and spec.get("steps"):
            return str(spec["steps"][0].get("health_path") or DEFAULT_HEALTH_PATH)
    return DEFAULT_HEALTH_PATH


def _write_json(path: Path, content: object) -> None:
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps(content))
    os.replace(partial, path)
