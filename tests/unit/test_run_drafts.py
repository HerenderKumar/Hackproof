"""`make drafts` (M6): the checks on drafted tests, and the suite's output."""

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog

from core.drafting import Drafted
from core.llm import LLMClient, replay_transport, request_key
from core.settings import Settings, get_settings
from core.specs import ApiSpec, UiSpec
from scripts import run_drafts
from scripts.fixtures import FIXTURES_DIR, load_drafting_cases, load_fixtures
from scripts.run_drafts import (
    check_case,
    check_fixture,
    check_planted,
    draft_suite,
    prune_recordings,
    remembering,
    select,
    specs_to_run,
    start_text,
)
from tests import spec_examples
from tests.llm_fakes import Scripted, draft_answer, draft_entry

FIXTURES = {f.id: f for f in load_fixtures()}
(D01,) = load_drafting_cases()
SETTINGS = Settings(
    _env_file=None,  # type: ignore[call-arg]
    llm_mode="replay",
    draft_model="draft-model",
    draft_max_tokens=4000,
)
UI = Drafted("c1", "ui_workflow", "ui_pending", "", None)


@pytest.fixture(autouse=True)
def quiet_logs() -> Iterator[None]:
    """The suite's printed lines are its output; `make drafts` logs warnings only."""
    with structlog.testing.capture_logs():
        yield


def drafted(document: dict[str, Any], claim_id: str = "c1") -> Drafted:
    if document.get("kind") == "ui":
        ui = UiSpec.model_validate({**document, "claim_id": claim_id})
        spec = ui.model_dump(by_alias=True, exclude_none=True)
    else:
        spec = ApiSpec.model_validate({**document, "claim_id": claim_id}).model_dump(by_alias=True)
    return Drafted(claim_id, spec["claim_type"], "draft", "", spec)


def unclear(reason: str = "The claim names no endpoint to call.") -> Drafted:
    return Drafted("c1", "calculation", "needs_clarification", reason, None)


def own_drafts(fixture_id: str) -> list[Drafted]:
    """The fixture's hand-written tests, as if they had been drafted."""
    return [drafted(spec, spec["claim_id"]) for spec in FIXTURES[fixture_id].specs]


# --- a fixture's drafts --------------------------------------------------------------------------


def test_every_fixtures_own_tests_would_pass_as_drafts() -> None:
    for fixture in FIXTURES.values():
        drafts = own_drafts(fixture.id) or [UI]
        assert check_fixture(fixture, drafts) == [], fixture.id


def test_a_draft_typed_differently_from_the_fixture_is_named() -> None:
    assert check_fixture(FIXTURES["F01"], [drafted(spec_examples.F02)]) == [
        "c1: typed create_update, the fixture says calculation"
    ]
    assert check_fixture(FIXTURES["F05"], [drafted(spec_examples.F01)]) == [
        "c1: typed calculation, the fixture says ui_workflow"
    ]


def test_a_draft_failing_the_validator_is_named() -> None:
    weak = copy.deepcopy(spec_examples.F01)
    weak["steps"][0]["assertions"] = [{"op": "status_equals", "value": 200}]

    (problem,) = check_fixture(FIXTURES["F01"], [drafted(weak)])

    assert problem.startswith("c1: a calculation test checks the result against a case value")


def test_an_api_claim_without_a_draft_is_named() -> None:
    assert check_fixture(FIXTURES["F01"], [unclear("Which endpoint?")]) == [
        "c1: needs_clarification, not a draft: Which endpoint?"
    ]
    assert check_fixture(FIXTURES["F09"], own_drafts("F09")[:1]) == ["c2: no answer"]


# --- the planted instruction ---------------------------------------------------------------------


def test_the_start_text_can_leave_the_planted_sentence_out() -> None:
    fixture = FIXTURES["F09"]
    planted, clean = start_text(fixture), start_text(fixture, without_planted=True)

    assert fixture.start["planted"] in planted.explanation
    assert fixture.start["planted"] not in clean.explanation
    assert clean.explanation == planted.explanation.replace(fixture.start["planted"], "").strip()
    assert (clean.problem_statement, clean.claims) == (planted.problem_statement, fixture.claims)


def test_the_planted_instruction_must_not_change_the_structure() -> None:
    clean = own_drafts("F09")
    other_values = copy.deepcopy(own_drafts("F09"))
    assert other_values[1].spec is not None
    other_values[1].spec["final_cases"][0]["input"]["c"] = 30
    obeyed = copy.deepcopy(spec_examples.F01) | {"claim_type": "create_update"}
    obeyed["steps"] = [
        {
            "id": "home",
            "request": {"method": "GET", "path": "/"},
            "assertions": [{"op": "status_equals", "value": 200}],
        }
    ]
    steered = [clean[0], Drafted("c2", "create_update", "draft", "", obeyed | {"claim_id": "c2"})]

    other_assertions = copy.deepcopy(own_drafts("F09"))
    assert other_assertions[1].spec is not None
    other_assertions[1].spec["steps"][0]["assertions"].append({"op": "exists", "path": "$.f"})

    one_more_step = copy.deepcopy(other_assertions)
    assert one_more_step[1].spec is not None
    again = copy.deepcopy(one_more_step[1].spec["steps"][0]) | {"id": "convert_again"}
    again["request"] = {"method": "GET", "path": "/convert?c={{case.input.c}}"}
    one_more_step[1].spec["steps"].insert(1, again)

    # Two drafts of one text differ by chance in values, assertions and set-up steps.
    assert check_planted(other_values, clean) == []
    assert check_planted(other_assertions, clean) == []
    assert check_planted(one_more_step, clean) == []
    assert check_planted(steered, clean) == [
        "c2: the planted instruction changed the test: GET / instead of "
        "GET /convert, GET /export.csv"
    ]
    refused = Drafted("c2", "create_update", "needs_clarification", "Too vague.", None)
    assert check_planted([clean[0], refused], clean) == [
        "c2: the planted instruction changed the test: needs_clarification create_update "
        "instead of draft create_update"
    ]
    assert check_planted(clean[:1], clean) == [
        "c2: the planted instruction changed the test: no answer with it"
    ]


# --- the vague claim -----------------------------------------------------------------------------


def test_a_vague_claim_must_need_clarification() -> None:
    assert check_case(D01, [unclear()]) == []
    assert check_case(D01, [drafted(spec_examples.F01)]) == [
        "c1: expected needs_clarification, got draft"
    ]
    assert check_case(D01, [unclear(reason="")]) == ["c1: needs_clarification without a reason"]
    assert check_case(D01, []) == ["c1: no answer"]


# --- the suite -----------------------------------------------------------------------------------


async def test_the_suite_prints_a_line_per_fixture_and_fails_on_a_broken_check(
    capsys: pytest.CaptureFixture[str],
) -> None:
    good = draft_answer(draft_entry(spec_examples.F01))
    transport = Scripted(good, copy.deepcopy(good))

    ok, drafts = await draft_suite(LLMClient(transport), SETTINGS, [FIXTURES["F01"]], [D01])

    assert capsys.readouterr().out.splitlines() == [
        "F01  c1=draft calculation  ok",
        "D01  c1=draft calculation  FAILED",
        "    c1: expected needs_clarification, got draft",
        "1 of 2 start texts drafted as expected",
    ]
    assert ok is False
    assert drafts["F01"] == [drafted(spec_examples.F01)]
    assert [body["model"] for body in transport.bodies] == ["draft-model", "draft-model"]


async def test_a_fixture_with_a_planted_instruction_is_drafted_with_and_without_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = FIXTURES["F09"]
    answer = draft_answer(*(draft_entry(spec, spec["claim_id"]) for spec in fixture.specs))
    transport = Scripted(answer, copy.deepcopy(answer))

    ok, _ = await draft_suite(LLMClient(transport), SETTINGS, [fixture], [])

    with_it, without_it = transport.user_messages()
    assert fixture.start["planted"] in with_it and fixture.start["planted"] not in without_it
    assert capsys.readouterr().out.splitlines() == [
        "F09  c1=draft calculation, c2=draft create_update  ok",
        "1 of 1 start texts drafted as expected",
    ]
    assert ok is True


async def test_a_missing_recording_is_reported_and_the_suite_goes_on(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    client = LLMClient(replay_transport(tmp_path))

    ok, drafts = await draft_suite(client, SETTINGS, [FIXTURES["F01"], FIXTURES["F07"]], [])

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("F01  ERROR  ReplayMissing: draft: no recorded reply")
    assert lines[1].startswith("F07  ERROR  ReplayMissing")
    assert (ok, drafts) == (False, {})


def test_only_drafted_tests_are_run_against_the_apps() -> None:
    draft = drafted(spec_examples.F01)

    assert specs_to_run([draft, UI, unclear()]) == [draft.spec]


# --- selecting and recording ---------------------------------------------------------------------


def test_only_the_named_fixtures_and_cases_are_drafted() -> None:
    fixtures, cases = list(FIXTURES.values()), [D01]

    assert select(fixtures, cases, []) == (fixtures, cases)
    assert select(fixtures, cases, ["F07", "D01"]) == ([FIXTURES["F07"]], [D01])
    assert select(fixtures, cases, ["F02"]) == ([FIXTURES["F02"]], [])
    with pytest.raises(ValueError, match="F77"):
        select(fixtures, cases, ["F01", "F77"])


def recording(tool: str) -> str:
    request = {"tools": [{"type": "function", "function": {"name": tool}}], "messages": []}
    return json.dumps({"request": request, "response": {}})


async def test_recordings_no_request_used_are_removed(tmp_path: Path) -> None:
    used: set[str] = set()
    transport = remembering(Scripted({"reply": 1}), used)
    body = {"model": "m", "messages": []}
    for name in (request_key(body), "0" * 64):
        (tmp_path / f"{name}.json").write_text(recording("submit_tests"))
    (tmp_path / "README.md").write_text("recordings")

    assert await transport(body) == {"reply": 1}

    assert used == {request_key(body)}
    assert prune_recordings(tmp_path, used, "submit_tests") == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["README.md", f"{request_key(body)}.json"]


def test_pruning_leaves_the_other_suites_recordings(tmp_path: Path) -> None:
    (tmp_path / f"{'a' * 64}.json").write_text(recording("submit_tests"))
    (tmp_path / f"{'b' * 64}.json").write_text(recording("submit_audit"))
    (tmp_path / f"{'c' * 64}.json").write_text("not json")

    assert prune_recordings(tmp_path, set(), "submit_tests") == 1

    assert sorted(p.stem[0] for p in tmp_path.glob("*.json")) == ["b", "c"]


# --- the recordings ------------------------------------------------------------------------------


def test_the_recorded_drafts_pass_every_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The committed replies in `fixtures/llm` answer every request `make drafts` sends: a
    changed prompt, tool or start text needs a new recording (`make drafts DRAFTS_MODE=record`)."""
    monkeypatch.setenv("LLM_MODE", "replay")
    monkeypatch.setenv("LLM_REPLAY_DIR", str(FIXTURES_DIR / "llm"))
    monkeypatch.setenv("DRAFT_MODEL", "anthropic/claude-sonnet-5.5")
    monkeypatch.setenv("DRAFT_MAX_TOKENS", "16000")
    monkeypatch.setattr(run_drafts, "configure_logging", lambda **_: None)
    get_settings.cache_clear()
    try:
        assert run_drafts.main([]) == 0
    finally:
        get_settings.cache_clear()

    assert capsys.readouterr().out.splitlines()[-1] == "11 of 11 start texts drafted as expected"
