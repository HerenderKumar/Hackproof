"""The spec schema (sections 6.1 and 14): structure, the case rules, hashes and modes.

Case rules (the human, 2026-11-13): one practice case the team sees, three hidden cases of which
two are edge cases, and no hidden input repeating another input.
"""

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from core.hashing import hash_json
from core.specs import (
    ApiSpec,
    ApiSpecShape,
    StartupSpec,
    UiSpec,
    for_mode,
    parse_spec,
    spec_hash,
    test_spec_hash,
)

P1 = {"id": "p1", "input": {"total": 100, "people": 4}, "expected": {}, "boundary": False}
F1 = {"id": "f1", "input": {"total": 100.00, "people": 3}, "expected": {}, "boundary": False}
F2 = {"id": "f2", "input": {"total": 0.01, "people": 2}, "expected": {}, "boundary": True}
F3 = {"id": "f3", "input": {"total": 1000, "people": 7}, "expected": {}, "boundary": True}
P1_OTHER = {"id": "p2", "input": {"total": 50, "people": 5}, "expected": {}, "boundary": False}
F4 = {"id": "f4", "input": {"total": 250, "people": 6}, "expected": {}, "boundary": True}


def api_spec(
    *,
    path: str = "/split",
    practice: list[dict[str, Any]] | None = None,
    hidden: list[dict[str, Any]] | None = None,
    final_total: Any = None,
) -> dict[str, Any]:
    """F01's spec as plain JSON (plan, Task 8)."""
    final_cases = copy.deepcopy(hidden if hidden is not None else [F1, F2, F3])
    if final_total is not None:
        final_cases[0]["input"]["total"] = final_total
    return {
        "kind": "api",
        "claim_id": "c1",
        "claim_type": "calculation",
        "steps": [
            {
                "id": "split",
                "request": {
                    "method": "POST",
                    "path": path,
                    "json": {"total": "{{case.input.total}}", "people": "{{case.input.people}}"},
                },
                "save": {},
                "assertions": [
                    {"op": "status_equals", "value": 200},
                    {"op": "length_equals", "path": "$.shares", "value": "{{case.input.people}}"},
                    {
                        "op": "sum_equals",
                        "path": "$.shares[*]",
                        "value": "{{case.input.total}}",
                        "tolerance": 0,
                    },
                ],
            }
        ],
        "practice_cases": copy.deepcopy(practice if practice is not None else [P1]),
        "final_cases": final_cases,
    }


def test_a_valid_api_spec_parses() -> None:
    spec = parse_spec(api_spec())

    assert isinstance(spec, ApiSpec)
    assert spec.steps[0].request.json_body == {
        "total": "{{case.input.total}}",
        "people": "{{case.input.people}}",
    }


def test_the_request_body_keeps_its_json_name() -> None:
    dumped = parse_spec(api_spec()).model_dump(by_alias=True)

    assert "json" in dumped["steps"][0]["request"]


@pytest.mark.parametrize(
    "path", ["http://evil.example/x", "//evil.example/x", "split", "", "/a\\b"]
)
def test_a_path_can_only_name_a_route_on_the_app(path: str) -> None:
    with pytest.raises(ValidationError):
        parse_spec(api_spec(path=path))


@pytest.mark.parametrize("practice", [[], [P1, P1_OTHER]])
def test_there_is_exactly_one_practice_case(practice: list[dict[str, Any]]) -> None:
    with pytest.raises(ValidationError, match="practice"):
        parse_spec(api_spec(practice=practice))


@pytest.mark.parametrize(
    "hidden",
    [
        [F1, F2],
        [F1, F2, F3, F4],
        [F1, F2 | {"boundary": False}, F3],
        [F1 | {"boundary": True}, F2, F3],
    ],
    ids=["two", "four", "one-edge-case", "three-edge-cases"],
)
def test_there_are_three_hidden_cases_two_of_them_edge_cases(
    hidden: list[dict[str, Any]],
) -> None:
    with pytest.raises(ValidationError, match="hidden"):
        parse_spec(api_spec(hidden=hidden))


def test_a_hidden_input_cannot_repeat_the_practice_input() -> None:
    with pytest.raises(ValidationError, match="repeat"):
        parse_spec(api_spec(hidden=[F1 | {"input": P1["input"]}, F2, F3]))


def test_hidden_inputs_cannot_repeat_each_other() -> None:
    with pytest.raises(ValidationError, match="repeat"):
        parse_spec(api_spec(hidden=[F1, F2, F3 | {"input": F2["input"]}]))


def test_case_ids_are_unique_across_both_sets() -> None:
    with pytest.raises(ValidationError, match="unique"):
        parse_spec(api_spec(hidden=[F1 | {"id": "p1"}, F2, F3]))


def test_an_unknown_assertion_op_is_rejected() -> None:
    spec = api_spec()
    spec["steps"][0]["assertions"][0]["op"] = "is_good"

    with pytest.raises(ValidationError):
        parse_spec(spec)


def test_a_spec_needs_at_least_one_step() -> None:
    with pytest.raises(ValidationError):
        parse_spec(api_spec() | {"steps": []})


def test_the_shape_alone_accepts_a_spec_trimmed_to_one_mode() -> None:
    assert ApiSpecShape.model_validate(for_mode(api_spec(), "final")).practice_cases == []


def test_for_mode_empties_only_the_other_set_and_copies() -> None:
    spec = api_spec()

    practice = for_mode(spec, "practice")
    final = for_mode(spec, "final")

    assert practice["final_cases"] == []
    assert practice["practice_cases"] == spec["practice_cases"]
    assert final["practice_cases"] == []
    assert final["final_cases"] == spec["final_cases"]
    assert len(spec["final_cases"]) == 3
    assert len(spec["practice_cases"]) == 1


def test_spec_hash_covers_the_rules_and_cases_only() -> None:
    spec = api_spec()
    keys = ("kind", "claim_type", "steps", "practice_cases", "final_cases")

    assert spec_hash(spec) == hash_json({k: spec[k] for k in keys})
    assert spec_hash(spec | {"claim_id": "c9"}) == spec_hash(spec)
    assert spec_hash(api_spec(final_total=250.00)) != spec_hash(spec)


def test_test_spec_hash_ignores_order() -> None:
    assert test_spec_hash(["b", "a"]) == test_spec_hash(["a", "b"]) == hash_json(["a", "b"])


def test_parse_spec_dispatches_on_kind() -> None:
    startup = {"kind": "startup", "claim_type": "startup", "steps": [{"health_path": "/"}]}
    ui = {"kind": "ui", "claim_id": "c2", "claim_type": "ui_workflow", "steps": []}

    assert isinstance(parse_spec(startup), StartupSpec)
    assert isinstance(parse_spec(ui), UiSpec)
    with pytest.raises(ValidationError):
        parse_spec({"kind": "graphql"})
