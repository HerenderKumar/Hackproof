"""The code validator (section 6.1's minimums per claim type): each rule, met and broken."""

import copy
from typing import Any

import pytest

from core.spec_rules import problems
from core.specs import ApiSpec, StartupSpec, parse_spec
from scripts.fixtures import load_fixtures
from tests import spec_examples


def check(document: dict[str, Any]) -> list[str]:
    return problems(ApiSpec.model_validate(document))


def example(name: str) -> dict[str, Any]:
    document: dict[str, Any] = copy.deepcopy(getattr(spec_examples, name))
    return document


def cases(document: dict[str, Any]) -> list[dict[str, Any]]:
    return [*document["practice_cases"], *document["final_cases"]]


def test_every_hand_written_fixture_spec_meets_the_rules() -> None:
    specs = [spec for fixture in load_fixtures() for spec in fixture.specs]

    assert {spec["claim_type"] for spec in specs} == {
        "calculation", "create_update", "classification", "authentication", "ui_workflow",
    }  # fmt: skip
    for spec in specs:
        parsed = parse_spec(spec)
        assert not isinstance(parsed, StartupSpec)
        assert problems(parsed) == []


# --- every test ----------------------------------------------------------------------------------


def test_a_placeholder_must_resolve_in_every_case() -> None:
    spec = example("F01")
    spec["steps"][0]["request"]["json"]["size"] = "{{case.input.size}}"
    for case in cases(spec):
        if case["id"] != "f2":
            case["input"]["size"] = "large"

    assert check(spec) == [
        'step "split" uses {{case.input.size}}, but input "size" is missing from case f2'
    ]


def test_an_expected_value_must_resolve_in_every_case() -> None:
    spec = example("F07")
    del spec["final_cases"][0]["expected"]["words"]
    del spec["final_cases"][2]["expected"]["words"]

    assert check(spec) == [
        'step "count" uses {{case.expected.words}}, but expected "words" is missing from '
        "case f1, f3"
    ]


@pytest.mark.parametrize("where", ["path", "query", "headers", "assertion_path", "nested_json"])
def test_placeholders_are_found_wherever_a_step_can_hold_one(where: str) -> None:
    spec = example("F02")
    read = spec["steps"][1]
    if where == "path":
        read["request"]["path"] = "/items/{{saved.id}}/{{case.input.gone}}"
    elif where == "query":
        read["request"]["query"] = {"q": "name is {{case.input.gone}}"}
    elif where == "headers":
        read["request"]["headers"] = {"X-Trace": "{{case.input.gone}}"}
    elif where == "assertion_path":
        read["assertions"][1]["path"] = "$.{{case.input.gone}}"
    else:
        read["request"]["json"] = {"filters": [{"name": "{{ case.input.gone }}"}]}

    assert check(spec) == [
        'step "read" uses {{case.input.gone}}, but input "gone" is missing from case p1, f1, f2, f3'
    ]


def test_a_placeholder_names_a_case_value_or_a_saved_value() -> None:
    spec = example("F01")
    spec["steps"][0]["request"]["json"]["total"] = "{{input.total}}"

    assert check(spec) == [
        'step "split" uses {{input.total}}, which is not a {{case.input.name}}, '
        "{{case.expected.name}} or {{saved.name}} placeholder"
    ]


@pytest.mark.parametrize("step", ["a_later_step", "the_saving_step"])
def test_a_saved_value_must_be_saved_by_an_earlier_step(step: str) -> None:
    spec = example("F02")
    if step == "a_later_step":
        spec["steps"][1]["request"]["path"] = "/items/{{saved.key}}"
        expected = 'step "read" uses {{saved.key}}, but no earlier step saves "key"'
    else:
        spec["steps"][0]["request"]["path"] = "/items/{{saved.id}}"
        expected = 'step "create" uses {{saved.id}}, but no earlier step saves "id"'

    assert check(spec) == [expected]


def test_every_step_asserts_its_status() -> None:
    spec = example("F02")
    spec["steps"][0]["assertions"] = [{"op": "exists", "path": "$.id"}]

    assert check(spec) == [
        'step "create" does not assert its status: add a status_equals assertion'
    ]


@pytest.mark.parametrize(
    ("where", "path"),
    [
        ("assertion", "$.items[?(@.name == 'rice')]"),
        ("save", "$.id[?(@ > 0)]"),
        ("assertion", "$.quantities[?(@ < {{case.input.quantity}})]"),
    ],
)
def test_a_path_the_runner_cannot_read_is_a_problem(where: str, path: str) -> None:
    spec = example("F02")
    if where == "assertion":
        spec["steps"][1]["assertions"][1]["path"] = path
        step = "read"
    else:
        spec["steps"][0]["save"] = {"id": path}
        step = "create"

    assert check(spec) == [
        f'step "{step}" has a path the runner cannot read: {path} (a path is a plain JSONPath: '
        "field names, list indexes and [*])"
    ]


def test_a_placeholder_in_a_path_may_stand_for_a_name_or_an_index() -> None:
    spec = example("F02")
    spec["steps"][1]["assertions"][1]["path"] = "$.{{case.input.name}}"
    spec["steps"][1]["assertions"][2]["path"] = "$.history[{{case.input.quantity}}].quantity"

    assert check(spec) == []


# --- each claim type -----------------------------------------------------------------------------


def test_a_calculation_compares_with_a_case_value() -> None:
    spec = example("F01")
    spec["steps"][0]["assertions"][1]["value"] = 4
    spec["steps"][0]["assertions"][2]["value"] = 100

    assert check(spec) == [
        "a calculation test checks the result against a case value: no assertion's value uses "
        "{{case.input.name}} or {{case.expected.name}}"
    ]


def test_a_case_value_as_the_status_is_not_a_checked_result() -> None:
    spec = example("F07")
    spec["steps"][0]["assertions"] = [{"op": "status_equals", "value": "{{case.expected.words}}"}]

    (problem,) = check(spec)
    assert problem.startswith("a calculation test checks the result against a case value")


@pytest.mark.parametrize("weakness", ["status_only", "one_step"])
def test_a_create_update_reads_back_more_than_a_status(weakness: str) -> None:
    spec = example("F02")
    if weakness == "status_only":
        del spec["steps"][1]["assertions"][1:]
        expected = (
            'a create_update test reads the change back: no step after "create" asserts more '
            "than its status"
        )
    else:
        del spec["steps"][1]
        expected = (
            "a create_update test needs at least two steps: one that makes the change and a "
            "later one that reads it back"
        )

    assert check(spec) == [expected]


def test_a_classification_expects_two_different_results() -> None:
    spec = example("F03")
    for case in spec["final_cases"]:
        case["expected"] = {"label": "negative"}

    assert check(spec) == [
        "a classification test's hidden cases expect at least two different results, but "
        "f1, f2, f3 all expect the same"
    ]


def test_a_classification_compares_with_the_expected_result() -> None:
    spec = example("F03")
    spec["steps"][0]["assertions"][1]["value"] = "{{case.input.text}}"

    assert check(spec) == [
        "a classification test checks the result against the case: no assertion's value uses "
        "{{case.expected.name}}"
    ]


@pytest.mark.parametrize("missing", ["refused", "allowed", "apart"])
def test_an_authentication_test_is_refused_and_allowed(missing: str) -> None:
    spec = example("F04")
    if missing == "refused":
        del spec["steps"][2]
        expected = "an authentication test needs a step that expects 401 or 403 (access refused)"
    elif missing == "allowed":
        del spec["steps"][:2]
        expected = "an authentication test needs a step that expects a 2xx status (access allowed)"
    else:  # one step cannot be both the allowed one and the refused one
        del spec["steps"][:2]
        spec["steps"][0]["assertions"].append({"op": "status_equals", "value": 200})
        expected = "an authentication test needs a step that expects a 2xx status (access allowed)"

    assert check(spec) == [expected]


def test_a_403_is_a_refusal_too() -> None:
    spec = example("F04")
    spec["steps"][2]["assertions"] = [{"op": "status_equals", "value": 403}]

    assert check(spec) == []
