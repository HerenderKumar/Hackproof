"""From what happened in a run to `test_results` rows (M5 design, section 6).

The runner reports evidence; this maps it to the four verdicts of section 6.3. The app's own
failures are `fail`, including an app that stopped answering mid-test. HackProof's own problems
(a timeout, a broken spec, a missing result, a crashed runner) are `unverified`, never `fail`.
An app that never started blocks every test the run carried, and that is never shown as the
claim failing.
"""

from typing import TYPE_CHECKING, Any
from uuid import UUID

from core.verdicts import Verdict
from worker.run_specs import NotRunnable, RunPlan

if TYPE_CHECKING:  # only for the annotation: worker.run_job imports this module
    from worker.run_job import RunReport

_APP_STOPPED = "connection_refused"


def result_rows(report: "RunReport", plan: RunPlan) -> list[dict[str, Any]]:
    """One row per runnable spec and case, plus one per spec that could not run."""
    rows = [_not_runnable(item) for item in plan.not_runnable]
    specs = {str(spec["spec_id"]): spec for spec in plan.runnable}
    run = report.run

    if run is None or run.startup_status != "ok":
        reason = run.startup_detail if run is not None else f"not started: {report.build.detail}"
        for spec_id in specs:
            rows.append(_row(spec_id, None, "blocked", error=f"the app did not start: {reason}"))
        return rows

    reported: set[str] = set()
    for entry in (run.results or {}).get("tests", []):
        spec_id = str(entry.get("spec_id"))
        spec = specs.get(spec_id)
        if spec is None:
            continue
        reported.add(spec_id)
        rows.append(_from_entry(entry, _case(spec, plan, entry.get("case_id"))))

    missing = "the run timed out" if run.timed_out else "no result from the runner"
    for spec_id in specs.keys() - reported:
        rows.append(_row(spec_id, None, "unverified", error=missing))
    return rows


def _from_entry(entry: dict[str, Any], case: dict[str, Any] | None) -> dict[str, Any]:
    status, error = entry.get("status"), entry.get("error") or {}
    verdict: Verdict
    if status == "pass":
        verdict, message = "pass", None
    elif status == "fail":
        verdict, message = "fail", None
    elif error.get("type") == _APP_STOPPED:
        verdict, message = "fail", error.get("message")
    else:
        verdict, message = "unverified", error.get("message") or "the runner reported an error"
    return _row(
        str(entry.get("spec_id")),
        entry.get("case_id"),
        verdict,
        error=message,
        expected=case,
        actual={"steps": entry.get("steps", [])},
        assertion_results=entry.get("assertions", []),
        duration_ms=entry.get("duration_ms"),
    )


def _case(spec: dict[str, Any], plan: RunPlan, case_id: str | None) -> dict[str, Any] | None:
    cases: list[dict[str, Any]] = spec.get(f"{plan.mode}_cases", [])
    return next((case for case in cases if case.get("id") == case_id), None)


def _not_runnable(item: NotRunnable) -> dict[str, Any]:
    return _row(str(item.spec_id), None, "unverified", error=item.reason)


def _row(
    spec_id: str,
    case_id: str | None,
    verdict: Verdict,
    *,
    error: str | None = None,
    expected: Any = None,
    actual: Any = None,
    assertion_results: Any = None,
    duration_ms: int | None = None,
) -> dict[str, Any]:
    return {
        "spec_id": UUID(spec_id),
        "case_id": case_id,
        "verdict": verdict,
        "expected": expected,
        "actual": actual,
        "assertion_results": assertion_results,
        "duration_ms": duration_ms,
        "error": error,
    }
