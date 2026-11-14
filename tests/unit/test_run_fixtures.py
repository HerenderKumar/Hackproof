"""`make fixtures` at M4: build and start every variant, compare with `expected.json`.

The pipeline is injected; the real one (snapshot, build, run) needs Docker and runs in the
worker image.
"""

import dataclasses
from pathlib import Path

import pytest

from scripts import run_fixtures
from scripts.fixtures import Fixture, Variant, load_fixtures
from scripts.run_fixtures import (
    Stages,
    compare,
    reasons_of,
    select_variants,
    stages_of,
    verdicts_of,
)
from worker.build import BuildResult
from worker.run_job import RunReport
from worker.run_specs import RunPlan
from worker.sandbox import RunOutcome

FIXTURES = load_fixtures()


def names(selected: list[tuple[Fixture, Variant]]) -> list[str]:
    return [variant.remote_name for _, variant in selected]


def built(status: str = "ok") -> BuildResult:
    return BuildResult(
        status=status,  # type: ignore[arg-type]
        method="railpack",
        exit_code=0 if status == "ok" else 1,
        image="hp/x:y" if status == "ok" else None,
        image_digest="sha256:00" if status == "ok" else None,
        build_config={},
        log_path=Path("/logs/build.log"),
        detail=None if status == "ok" else "railpack build failed (exit 1)",
    )


def ran(status: str = "ok", detail: str = "HTTP 200 after 1.0s on port 8080") -> RunOutcome:
    return RunOutcome(
        startup_status=status,  # type: ignore[arg-type]
        startup_detail=detail,
        flags_used={"root_fallback": False, "candidate_ports": [8080], "port": 8080},
        results=None,
        app_log_path=Path("/logs/app.log"),
        timed_out=False,
    )


# --- choosing variants ---------------------------------------------------------------------------


def test_without_a_filter_every_variant_runs() -> None:
    assert len(select_variants(FIXTURES, [])) == 19


def test_a_filter_matches_fixture_ids_and_variant_names() -> None:
    selected = select_variants(FIXTURES, ["F08", "F06-fastapi-distance-broken"])

    assert names(selected) == [
        "F06-fastapi-distance-broken",
        "F08-dockerfile-temperature-good",
        "F08-dockerfile-temperature-broken",
    ]


def test_a_filter_that_matches_nothing_is_an_error() -> None:
    with pytest.raises(ValueError, match="F99"):
        select_variants(FIXTURES, ["F99"])


# --- comparing -----------------------------------------------------------------------------------


def test_the_stages_of_a_run_that_started() -> None:
    assert stages_of(RunReport(built(), ran())) == Stages(
        build="ok",
        startup="ok",
        detail="HTTP 200 after 1.0s on port 8080",
        log_path="/logs/app.log",
    )


def test_fallbacks_and_another_port_are_noted() -> None:
    fallback = dataclasses.replace(
        ran(), flags_used={"root_fallback": True, "candidate_ports": [8080, 3000], "port": 3000}
    )

    stages = stages_of(RunReport(built(), fallback))

    assert stages.notes == "root fallback, port 3000"


def test_a_strict_run_on_8080_has_no_notes() -> None:
    assert stages_of(RunReport(built(), ran())).notes == ""


def test_a_failed_build_skips_startup() -> None:
    assert stages_of(RunReport(built("fail"), None)) == Stages(
        build="fail",
        startup="skipped",
        detail="railpack build failed (exit 1)",
        log_path="/logs/build.log",
    )


def test_matching_stages_have_no_differences() -> None:
    assert compare({"build": "ok", "startup": "fail"}, Stages("ok", "fail", "")) == []


def test_each_differing_stage_is_named() -> None:
    assert compare({"build": "ok", "startup": "fail"}, Stages("fail", "skipped", "")) == [
        "build: expected ok, got fail",
        "startup: expected fail, got skipped",
    ]


# --- the suite -----------------------------------------------------------------------------------


async def test_the_suite_passes_when_every_variant_matches(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def pipeline(fixture: Fixture, variant: Variant) -> Stages:
        expected = variant.expected
        return Stages(expected["build"], expected["startup"], "as expected")

    code = await run_fixtures.run_suite(select_variants(FIXTURES, ["F08"]), pipeline)

    out = capsys.readouterr().out
    assert code == 0
    assert "F08-dockerfile-temperature-good" in out
    assert "2 of 2 variants match" in out


async def test_the_suite_fails_and_shows_the_difference_when_a_variant_does_not_match(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def pipeline(fixture: Fixture, variant: Variant) -> Stages:
        return Stages("ok", "ok", "HTTP 200 after 0.5s on port 8080")

    code = await run_fixtures.run_suite(select_variants(FIXTURES, ["F08"]), pipeline)

    out = capsys.readouterr().out
    assert code == 1
    assert "startup: expected fail, got ok" in out
    assert "1 of 2 variants match" in out


async def test_a_variant_whose_pipeline_raises_is_reported_and_the_suite_goes_on(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def pipeline(fixture: Fixture, variant: Variant) -> Stages:
        if variant.name == "good":
            raise RuntimeError("buildkit is down")
        return Stages("ok", "fail", "the app exited with code 1 before answering")

    code = await run_fixtures.run_suite(select_variants(FIXTURES, ["F08"]), pipeline)

    out = capsys.readouterr().out
    assert code == 1
    assert "RuntimeError: buildkit is down" in out
    assert "1 of 2 variants match" in out


# --- verdicts (M5) -------------------------------------------------------------------------------

EXPECT_FAIL = {"build": "ok", "startup": "ok", "verdicts": {"c1": "fail"}}


def test_matching_verdicts_have_no_differences() -> None:
    assert compare(EXPECT_FAIL, Stages("ok", "ok", "", verdicts={"c1": "fail"})) == []


def test_verdict_differences_are_named() -> None:
    assert compare(EXPECT_FAIL, Stages("ok", "ok", "", verdicts={"c1": "pass"})) == [
        "c1: expected fail, got pass"
    ]


def test_ui_claims_are_not_compared_before_m11() -> None:
    stages = Stages("ok", "ok", "", verdicts={"c1": "unverified"})
    assert compare(EXPECT_FAIL, stages, ui_claims={"c1"}) == []


def test_verdicts_come_from_the_same_mapping_as_the_job() -> None:
    spec_id = "00000000-0000-0000-0000-000000000001"
    plan = RunPlan("final", [{"spec_id": spec_id, "claim_id": "c1", "final_cases": []}], [])

    blocked = verdicts_of(RunReport(built("fail"), None), plan, ["c1", "c2"])

    assert blocked == {"c1": "blocked", "c2": "unverified"}


SPEC_ID = "00000000-0000-0000-0000-000000000001"
PLAN = RunPlan("final", [{"spec_id": SPEC_ID, "claim_id": "c1", "final_cases": []}], [])
STATUS_OK = {"op": "status_equals", "path": None, "expected": 200, "actual": 200, "passed": True}


def report_with(*entries: dict[str, object]) -> RunReport:
    return RunReport(built(), dataclasses.replace(ran(), results={"tests": list(entries)}))


def entry(case_id: str, status: str, **fields: object) -> dict[str, object]:
    return {"spec_id": SPEC_ID, "case_id": case_id, "status": status, **fields}


def test_a_failing_claim_is_explained_by_its_first_failed_assertion() -> None:
    wrong_sum = {"op": "sum_equals", "path": "$.shares[*]", "expected": "100.0",
                 "actual": "99.99", "passed": False, "note": None, "step": "split"}  # fmt: skip
    report = report_with(
        entry("f1", "pass", assertions=[STATUS_OK]),
        entry("f2", "fail", assertions=[STATUS_OK, wrong_sum]),
    )

    assert reasons_of(report, PLAN) == {
        "c1": "f2 split: sum_equals $.shares[*] expected 100.0, got 99.99"
    }


def test_a_note_or_an_error_explains_a_failure_when_there_is_one() -> None:
    not_json = {"op": "equals", "path": "$.label", "expected": "positive", "actual": None,
                "passed": False, "note": "the response is not JSON", "step": None}  # fmt: skip
    stopped = {"type": "connection_refused", "message": "the app stopped answering"}

    assert reasons_of(report_with(entry("f1", "fail", assertions=[not_json])), PLAN) == {
        "c1": "f1: equals $.label expected positive: the response is not JSON"
    }
    assert reasons_of(report_with(entry("f1", "error", error=stopped)), PLAN) == {
        "c1": "f1: the app stopped answering"
    }


def test_passing_and_blocked_claims_need_no_reason() -> None:
    assert reasons_of(report_with(entry("f1", "pass", assertions=[STATUS_OK])), PLAN) == {}
    assert reasons_of(RunReport(built("fail"), None), PLAN) == {}


async def test_the_suite_shows_why_a_claim_failed(capsys: pytest.CaptureFixture[str]) -> None:
    why = "f1 split: sum_equals $.shares[*] expected 100.0, got 99.99"

    async def pipeline(fixture: Fixture, variant: Variant) -> Stages:
        return Stages("ok", "ok", "up", verdicts={"c1": "fail"}, reasons={"c1": why})

    selected = select_variants(FIXTURES, ["F01-node-splitter-broken"])
    code = await run_fixtures.run_suite(selected, pipeline)

    assert code == 0
    assert f"c1=fail ({why})" in capsys.readouterr().out


async def test_the_suite_marks_ui_claims_as_pending(capsys: pytest.CaptureFixture[str]) -> None:
    async def pipeline(fixture: Fixture, variant: Variant) -> Stages:
        return Stages("ok", "ok", "up", verdicts={"c1": "unverified"})

    code = await run_fixtures.run_suite(select_variants(FIXTURES, ["F05"]), pipeline)

    out = capsys.readouterr().out
    assert code == 0
    assert "UI tests arrive in M11" in out
