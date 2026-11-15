# ruff: noqa: E501
"""From what happened in a run to `test_results` rows (M5 design, section 6's table)."""

import uuid
from pathlib import Path
from typing import Any

import pytest

from worker.build import BuildResult
from worker.results import result_rows
from worker.run_job import RunReport
from worker.run_specs import NotRunnable, RunPlan
from worker.sandbox import RunOutcome

ID1, ID2, ID3 = (uuid.UUID(int=n) for n in (1, 2, 3))
F1 = {"id": "f1", "input": {"total": 100, "people": 3}, "expected": {}, "boundary": False}
F2 = {"id": "f2", "input": {"total": 0.01, "people": 2}, "expected": {}, "boundary": True}


def spec(spec_id: uuid.UUID, cases: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"spec_id": str(spec_id), "kind": "api", "claim_id": "c1", "practice_cases": [],
            "final_cases": [F1, F2] if cases is None else cases}  # fmt: skip


def plan(*runnable: dict[str, Any], not_runnable: list[NotRunnable] | None = None) -> RunPlan:
    return RunPlan("final", list(runnable), not_runnable or [])


def built(ok: bool = True) -> BuildResult:
    return BuildResult(
        "ok" if ok else "fail", "railpack", 0 if ok else 1, "hp/t:a" if ok else None,
        "sha256:00" if ok else None, {}, Path("/l/build.log"), None if ok else "railpack build failed (exit 1)",
    )  # fmt: skip


def report(
    startup: str = "ok",
    tests: list[dict[str, Any]] | None = None,
    *,
    detail: str = "HTTP 200 after 1.0s on port 8080",
    timed_out: bool = False,
    results: bool = True,
) -> RunReport:
    outcome = RunOutcome(
        startup_status=startup,  # type: ignore[arg-type]
        startup_detail=detail,
        flags_used={},
        results={"runner_version": "1", "startup": {}, "tests": tests or []} if results else None,
        app_log_path=Path("/l/app.log"),
        timed_out=timed_out,
    )
    return RunReport(built(), outcome)


def entry(
    spec_id: uuid.UUID, case_id: str, status: str, error: dict[str, str] | None = None
) -> dict[str, Any]:
    assertion = {"op": "sum_equals", "path": "$.shares[*]", "expected": "100.0", "actual": "99.99",
                 "passed": status == "pass", "note": None, "step": "split"}  # fmt: skip
    return {
        "spec_id": str(spec_id), "case_id": case_id, "status": status, "error": error,
        "assertions": [assertion], "steps": [{"id": "split", "request": {}, "response": {}, "assertions": [assertion]}],
        "request": {}, "response": {}, "duration_ms": 12, "video_path": None, "trace_path": None,
    }  # fmt: skip


def verdicts(rows: list[dict[str, Any]]) -> set[tuple[uuid.UUID, str | None, str]]:
    return {(row["spec_id"], row["case_id"], row["verdict"]) for row in rows}


def test_an_app_that_never_started_blocks_every_test_it_would_have_run() -> None:
    gone = "the app exited with code 1 before answering"

    rows = result_rows(report("fail", detail=gone, results=False), plan(spec(ID1), spec(ID2)))

    assert verdicts(rows) == {(ID1, None, "blocked"), (ID2, None, "blocked")}
    assert {row["error"] for row in rows} == {f"the app did not start: {gone}"}


def test_a_failed_build_blocks_every_test() -> None:
    rows = result_rows(RunReport(built(ok=False), None), plan(spec(ID1)))

    assert verdicts(rows) == {(ID1, None, "blocked")}
    assert rows[0]["error"] == "the app did not start: not started: railpack build failed (exit 1)"


def test_runner_pass_and_fail_become_verdicts_with_their_evidence() -> None:
    tests = [entry(ID1, "f1", "fail"), entry(ID1, "f2", "pass")]

    rows = result_rows(report(tests=tests), plan(spec(ID1)))

    assert verdicts(rows) == {(ID1, "f1", "fail"), (ID1, "f2", "pass")}
    failed = next(row for row in rows if row["case_id"] == "f1")
    assert failed["expected"] == F1
    assert failed["assertion_results"][0]["actual"] == "99.99"
    assert failed["actual"] == {"steps": tests[0]["steps"]}
    assert failed["duration_ms"] == 12
    assert failed["error"] is None


def test_the_app_stopping_mid_test_is_a_fail() -> None:
    stopped = {"type": "connection_refused", "message": "the app stopped answering: reset"}

    rows = result_rows(report(tests=[entry(ID1, "f1", "error", stopped)]), plan(spec(ID1, [F1])))

    assert verdicts(rows) == {(ID1, "f1", "fail")}
    assert rows[0]["error"] == "the app stopped answering: reset"


@pytest.mark.parametrize("kind", ["timeout", "spec", "unsupported", "runner"])
def test_our_own_problems_are_unverified(kind: str) -> None:
    problem = {"type": kind, "message": f"a {kind} problem"}

    rows = result_rows(report(tests=[entry(ID1, "f1", "error", problem)]), plan(spec(ID1, [F1])))

    assert verdicts(rows) == {(ID1, "f1", "unverified")}
    assert rows[0]["error"] == f"a {kind} problem"


def test_a_spec_the_runner_did_not_report_is_unverified() -> None:
    rows = result_rows(report(tests=[entry(ID1, "f1", "pass")]), plan(spec(ID1, [F1]), spec(ID2)))

    assert verdicts(rows) == {(ID1, "f1", "pass"), (ID2, None, "unverified")}
    assert next(r for r in rows if r["spec_id"] == ID2)["error"] == "no result from the runner"


def test_a_run_that_timed_out_after_startup_is_unverified() -> None:
    rows = result_rows(report(timed_out=True, results=False), plan(spec(ID1)))

    assert verdicts(rows) == {(ID1, None, "unverified")}
    assert rows[0]["error"] == "the run timed out"


def test_a_test_that_was_never_runnable_stays_unverified_even_when_the_app_did_not_start() -> None:
    vague = NotRunnable(ID3, "c2", "which unit?")

    rows = result_rows(
        report("fail", detail="gone", results=False), plan(spec(ID1), not_runnable=[vague])
    )

    assert verdicts(rows) == {(ID1, None, "blocked"), (ID3, None, "unverified")}
    assert next(r for r in rows if r["spec_id"] == ID3)["error"] == "which unit?"


def test_results_for_specs_outside_the_plan_are_ignored() -> None:
    rows = result_rows(report(tests=[entry(ID2, "f1", "pass")]), plan(spec(ID1, [F1])))

    assert verdicts(rows) == {(ID1, None, "unverified")}


# --- UI cases (M11) ------------------------------------------------------------------------------


def ui_report(tests: list[dict[str, Any]], media: dict[Any, Any]) -> RunReport:
    outcome = RunOutcome(
        startup_status="ok", startup_detail="up", flags_used={},
        results={"runner_version": "1", "startup": {}, "tests": tests},
        app_log_path=Path("/l/app.log"), timed_out=False, media=media,
    )  # fmt: skip
    return RunReport(built(), outcome)


def ui_entry(case_id: str, status: str, error: dict[str, str] | None = None) -> dict[str, Any]:
    return entry(ID1, case_id, status, error) | {
        "kind": "ui", "video_path": "ui/../../etc/passwd", "page": {"outline": "- button \"Add\""},
    }  # fmt: skip


def test_a_ui_case_carries_the_files_the_worker_kept_not_the_runners_paths() -> None:
    kept = {
        "video": Path("/a/runs/x/ui/1/2/video.webm"),
        "trace": Path("/a/runs/x/ui/1/2/trace.zip"),
    }
    media = {(str(ID1), 2): kept}

    rows = result_rows(ui_report([ui_entry("f1", "pass"), ui_entry("f2", "fail")], media),
                       plan(spec(ID1)))  # fmt: skip

    by_case = {row["case_id"]: row for row in rows}
    assert (by_case["f1"]["video_path"], by_case["f1"]["trace_path"]) == (None, None)
    assert by_case["f2"]["video_path"] == "/a/runs/x/ui/1/2/video.webm"
    assert by_case["f2"]["trace_path"] == "/a/runs/x/ui/1/2/trace.zip"
    assert by_case["f2"]["verdict"] == "fail"
    assert by_case["f2"]["actual"]["page"] == {"outline": '- button "Add"'}


@pytest.mark.parametrize("kind", ["locator_not_found", "locator_ambiguous", "timeout"])
def test_a_control_the_page_did_not_offer_is_unverified(kind: str) -> None:
    error = {"type": kind, "message": "nothing on the page matched: Click the button named “Add”"}

    (row,) = result_rows(ui_report([ui_entry("f1", "error", error)], {}), plan(spec(ID1, [F1])))

    assert (row["verdict"], row["error"]) == ("unverified", error["message"])
