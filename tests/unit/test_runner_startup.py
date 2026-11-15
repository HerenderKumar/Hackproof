"""The runner's startup check (section 6.2) and its input and output files (section 14.4)."""

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from runner.main import load_request, load_request_text, run
from runner.startup import ProbeFailed, check_startup
from tests.spec_examples import F05, F07


class FakeTime:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeApp:
    """Answers probes from a script: `{url: [answer for round 1, round 2, ...]}`, where an
    answer is a status code or the reason no answer came. The last answer repeats."""

    def __init__(self, time: FakeTime, script: dict[str, list[int | str]], cost_s: float = 0.0):
        self.time = time
        self.script = script
        self.cost_s = cost_s
        self.asked: list[str] = []

    def __call__(self, url: str) -> int:
        self.asked.append(url)
        self.time.now += self.cost_s
        answers = self.script.get(url, ["connection refused"])
        round_index = min(self.asked.count(url) - 1, len(answers) - 1)
        answer = answers[round_index]
        if isinstance(answer, str):
            raise ProbeFailed(answer)
        return answer


def check(app: FakeApp, time: FakeTime, ports: list[int] | None = None, path: str = "/") -> dict:
    return check_startup(
        "http://app:8080",
        ports or [8080],
        path,
        timeout_s=60,
        probe=app,
        monotonic=time.monotonic,
        sleep=time.sleep,
    )


def test_an_app_that_answers_at_once_is_up() -> None:
    time = FakeTime()
    app = FakeApp(time, {"http://app:8080/": [200]})

    result = check(app, time)

    assert result == {
        "status": "ok",
        "detail": "HTTP 200 after 0.0s on port 8080",
        "port": 8080,
        "elapsed_ms": 0,
    }


def test_the_check_asks_once_a_second_until_the_app_answers() -> None:
    time = FakeTime()
    app = FakeApp(time, {"http://app:8080/": ["connection refused", "connection refused", 200]})

    result = check(app, time)

    assert result["status"] == "ok"
    assert result["elapsed_ms"] == 2000
    assert result["detail"] == "HTTP 200 after 2.0s on port 8080"
    assert time.sleeps == [1.0, 1.0]


def test_slow_answers_do_not_stretch_the_one_second_rhythm() -> None:
    time = FakeTime()
    app = FakeApp(time, {"http://app:8080/": ["timed out", 200]}, cost_s=0.25)

    check(app, time)

    assert time.sleeps == [0.75]


@pytest.mark.parametrize("status", [301, 401, 404, 499])
def test_any_status_below_500_counts_as_up(status: int) -> None:
    time = FakeTime()
    result = check(FakeApp(time, {"http://app:8080/": [status]}), time)
    assert result["status"] == "ok"


def test_an_app_that_only_answers_with_server_errors_is_not_up() -> None:
    time = FakeTime()
    app = FakeApp(time, {"http://app:8080/": [502]})

    result = check(app, time)

    assert result["status"] == "fail"
    assert result["port"] is None
    assert result["elapsed_ms"] == 60000
    assert result["detail"] == "no answer below HTTP 500 within 60 s (last: HTTP 502 on port 8080)"


def test_an_app_that_never_answers_fails_after_the_timeout() -> None:
    time = FakeTime()
    app = FakeApp(time, {})

    result = check(app, time)

    assert result["status"] == "fail"
    assert result["detail"] == (
        "nothing answered on port 8080 within 60 s (last: connection refused)"
    )
    assert len(app.asked) == 60
    assert time.now - 1000.0 == pytest.approx(60.0)


def test_each_candidate_port_is_tried_and_the_one_that_answers_is_reported() -> None:
    time = FakeTime()
    app = FakeApp(time, {"http://app:3000/health": ["connection refused", 200]})

    result = check(app, time, ports=[8080, 3000], path="/health")

    assert result["status"] == "ok"
    assert result["port"] == 3000
    assert app.asked[:4] == [
        "http://app:8080/health",
        "http://app:3000/health",
        "http://app:8080/health",
        "http://app:3000/health",
    ]


def test_the_first_candidate_wins_when_several_answer() -> None:
    time = FakeTime()
    app = FakeApp(time, {"http://app:8080/": [200], "http://app:3000/": [200]})

    assert check(app, time, ports=[8080, 3000])["port"] == 8080


def test_a_failure_names_every_port_it_tried() -> None:
    time = FakeTime()

    result = check(FakeApp(time, {}), time, ports=[8080, 3000])

    assert result["detail"].startswith("nothing answered on ports 8080, 3000 within 60 s")


# --- the runner's files --------------------------------------------------------------------------


def write_spec(work: Path, **overrides: object) -> None:
    spec = {
        "mode": "final",
        "base_url": "http://app:8080",
        "candidate_ports": [8080],
        "startup_timeout_s": 60,
        "specs": [
            {
                "kind": "startup",
                "claim_type": "startup",
                "steps": [{"health_path": "/healthz"}],
                "practice_cases": [],
                "final_cases": [],
            }
        ],
        **overrides,
    }
    (work / "spec.json").write_text(json.dumps(spec))


def test_the_runner_writes_its_startup_result_and_then_its_results(tmp_path: Path) -> None:
    write_spec(tmp_path)
    time = FakeTime()
    app = FakeApp(time, {"http://app:8080/healthz": [200]})

    code = run(tmp_path, probe=app, monotonic=time.monotonic, sleep=time.sleep)

    assert code == 0
    startup = json.loads((tmp_path / "startup.json").read_text())
    results = json.loads((tmp_path / "results.json").read_text())
    assert startup["status"] == "ok"
    assert results == {"runner_version": "1", "startup": startup, "tests": []}
    assert not list(tmp_path.glob("*.partial"))


def test_without_a_startup_spec_the_runner_checks_the_root_path(tmp_path: Path) -> None:
    write_spec(tmp_path, specs=[], candidate_ports=[8080, 5000])
    time = FakeTime()
    app = FakeApp(time, {"http://app:5000/": [200]})

    run(tmp_path, probe=app, monotonic=time.monotonic, sleep=time.sleep)

    assert json.loads((tmp_path / "results.json").read_text())["startup"]["port"] == 5000


def test_a_failed_startup_is_still_a_complete_result(tmp_path: Path) -> None:
    write_spec(tmp_path, startup_timeout_s=3)
    time = FakeTime()

    code = run(tmp_path, probe=FakeApp(time, {}), monotonic=time.monotonic, sleep=time.sleep)

    assert code == 0
    results = json.loads((tmp_path / "results.json").read_text())
    assert results["startup"]["status"] == "fail"
    assert results["tests"] == []


# --- after startup, the API tests ----------------------------------------------------------------


def recording_client(seen: list[str]) -> httpx.Client:
    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"words": 3})

    return httpx.Client(transport=httpx.MockTransport(answer))


def test_after_startup_the_runner_runs_the_tests_on_the_port_that_answered(tmp_path: Path) -> None:
    write_spec(
        tmp_path,
        candidate_ports=[8080, 3000],
        specs=[{"kind": "startup", "claim_type": "startup", "steps": [{"health_path": "/"}]},
               F07 | {"spec_id": "s7"}],
    )  # fmt: skip
    time, seen = FakeTime(), []
    app = FakeApp(time, {"http://app:3000/": [200]})

    run(
        tmp_path,
        probe=app,
        monotonic=time.monotonic,
        sleep=time.sleep,
        client=recording_client(seen),
    )

    results = json.loads((tmp_path / "results.json").read_text())
    assert [t["case_id"] for t in results["tests"]] == ["f1", "f2", "f3"]
    assert seen and all(url.startswith("http://app:3000/") for url in seen)


def test_no_tests_run_when_startup_fails(tmp_path: Path) -> None:
    write_spec(tmp_path, startup_timeout_s=2, specs=[F07 | {"spec_id": "s7"}])
    time, seen = FakeTime(), []

    run(tmp_path, probe=FakeApp(time, {}), monotonic=time.monotonic, sleep=time.sleep,
        client=recording_client(seen))  # fmt: skip

    assert json.loads((tmp_path / "results.json").read_text())["tests"] == []
    assert seen == []


def test_spec_numbers_reach_the_tests_with_their_exact_text(tmp_path: Path) -> None:
    (tmp_path / "spec.json").write_text(
        '{"mode": "final", "base_url": "http://app:8080", "candidate_ports": [8080],'
        ' "startup_timeout_s": 5, "specs": []}'
    )
    request = load_request(tmp_path)

    assert request["candidate_ports"] == [8080]
    assert load_request_text('{"x": 100.00}')["x"] == Decimal("100.00")


# --- after the API tests, the UI tests -----------------------------------------------------------


class FakeUiRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str, str]] = []

    def __call__(self, specs: list[dict], mode: str, app_url: str, work: Path) -> list[dict]:
        self.calls.append(([s["spec_id"] for s in specs if s["kind"] == "ui"], mode, app_url))
        return [{"spec_id": s["spec_id"], "case_id": "f1", "status": "pass"}
                for s in specs if s["kind"] == "ui"]  # fmt: skip


def test_ui_tests_run_after_the_api_tests_and_keep_the_specs_order(tmp_path: Path) -> None:
    ui = F05 | {"spec_id": "u5"}
    write_spec(tmp_path, specs=[ui, F07 | {"spec_id": "s7"}])
    time, browser = FakeTime(), FakeUiRunner()

    run(tmp_path, probe=FakeApp(time, {"http://app:8080/": [200]}), monotonic=time.monotonic,
        sleep=time.sleep, client=recording_client([]), ui_runner=browser)  # fmt: skip

    tests = json.loads((tmp_path / "results.json").read_text())["tests"]
    assert browser.calls == [(["u5"], "final", "http://app:8080")]
    assert [(t["spec_id"], t["case_id"]) for t in tests] == [
        ("u5", "f1"), ("s7", "f1"), ("s7", "f2"), ("s7", "f3"),
    ]  # fmt: skip


def test_a_run_without_a_ui_spec_never_starts_a_browser(tmp_path: Path) -> None:
    write_spec(tmp_path, specs=[F07 | {"spec_id": "s7"}])
    time, browser = FakeTime(), FakeUiRunner()

    run(tmp_path, probe=FakeApp(time, {"http://app:8080/": [200]}), monotonic=time.monotonic,
        sleep=time.sleep, client=recording_client([]), ui_runner=browser)  # fmt: skip

    assert browser.calls == []
