"""UI specs (section 14.2, M11 design parts 1 and 2): the shape, the sentences, the validator."""

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from core.spec_rules import problems
from core.specs import UiSpec, UiSpecShape, UiStep, for_mode, parse_spec, spec_hash
from core.ui_words import step_words
from tests import spec_examples


def f05() -> dict[str, Any]:
    document: dict[str, Any] = copy.deepcopy(spec_examples.F05)
    return document


def check(document: dict[str, Any]) -> list[str]:
    return problems(UiSpec.model_validate(document))


# --- shape ---------------------------------------------------------------------------------------


def test_f05_parses_as_a_ui_spec_and_meets_the_rules() -> None:
    spec = parse_spec(f05())

    assert isinstance(spec, UiSpec)
    assert [step.action for step in spec.steps] == [
        "goto", "fill", "click", "expect_text", "expect_value",
    ]  # fmt: skip
    assert check(f05()) == []


def test_ui_specs_keep_the_case_rules() -> None:
    document = f05()
    document["final_cases"][2]["boundary"] = False

    with pytest.raises(ValidationError, match="hidden"):
        parse_spec(document)


def test_the_shape_alone_accepts_one_modes_cases() -> None:
    assert UiSpecShape.model_validate(for_mode(f05(), "final")).practice_cases == []


def test_steps_without_an_id_are_numbered_by_position() -> None:
    document = f05()
    for step in document["steps"]:
        del step["id"]

    assert [step.id for step in UiSpec.model_validate(document).steps] == [
        "s1", "s2", "s3", "s4", "s5",
    ]  # fmt: skip


def test_step_ids_are_unique() -> None:
    document = f05()
    document["steps"][1]["id"] = "open"

    with pytest.raises(ValidationError, match="unique"):
        parse_spec(document)


def test_a_ui_test_has_at_most_twelve_steps() -> None:
    document = f05()
    document["steps"] = [{"action": "goto", "path": "/"}] * 13

    with pytest.raises(ValidationError):
        parse_spec(document)


@pytest.mark.parametrize(
    "step",
    [
        {"action": "goto"},
        {"action": "fill", "locator": {"label": "x"}},
        {"action": "click", "locator": {"label": "x"}, "value": "y"},
        {"action": "expect_text", "locator": {"label": "x"}},
        {"action": "press"},
        {"action": "hover", "locator": {"label": "x"}},
        {"action": "click", "locator": {"label": "x"}, "script": "alert(1)"},
    ],
    ids=["goto-no-path", "fill-no-value", "click-with-value", "text-no-text", "press-no-key",
         "unknown-action", "unknown-field"],
)  # fmt: skip
def test_a_step_has_exactly_its_actions_fields(step: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        UiStep.model_validate(step)


def test_press_takes_a_key_with_or_without_a_locator() -> None:
    assert UiStep.model_validate({"action": "press", "key": "Enter"}).locator is None
    UiStep.model_validate({"action": "press", "key": "a", "locator": {"label": "x"}})
    with pytest.raises(ValidationError, match="key"):
        UiStep.model_validate({"action": "press", "key": "Control+W"})


@pytest.mark.parametrize("path", ["http://evil.example/", "//evil.example/", "home", "/a\\b"])
def test_a_page_path_names_a_route_on_the_app(path: str) -> None:
    with pytest.raises(ValidationError, match="route on the app"):
        UiStep.model_validate({"action": "goto", "path": path})


@pytest.mark.parametrize(
    "locator",
    [
        {},
        {"label": "a", "text": "b"},
        {"name": "Add"},
        {"role": "spaceship", "name": "x"},
        {"label": ""},
        {"css": "#add"},
    ],
    ids=["none", "two", "name-without-role", "unknown-role", "empty", "css"],
)
def test_a_locator_finds_one_way(locator: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        UiStep.model_validate({"action": "click", "locator": locator})


def test_the_hash_covers_ui_steps() -> None:
    changed = f05()
    changed["steps"][2]["locator"]["name"] = "Save"

    assert spec_hash(changed) != spec_hash(f05())


# --- sentences -----------------------------------------------------------------------------------


def test_each_f05_step_reads_as_a_sentence() -> None:
    spec = UiSpec.model_validate(f05())

    assert [step_words(step) for step in spec.steps] == [
        "Open the page at /",
        "Type “{{case.input.item}}” into the field labelled “New item”",
        "Click the button named “Add”",
        "Check that the list named “Items” shows “{{case.input.item}}”",
        "Check that the field labelled “New item” is empty",
    ]


@pytest.mark.parametrize(
    ("step", "words"),
    [
        ({"action": "select", "locator": {"role": "combobox", "name": "Size"}, "value": "L"},
         "Choose “L” in the drop-down named “Size”"),
        ({"action": "press", "key": "Enter"}, "Press Enter"),
        ({"action": "press", "key": "Enter", "locator": {"placeholder": "Search"}},
         "Press Enter in the field with the hint “Search”"),
        ({"action": "wait_for", "locator": {"text": "Saved"}},
         "Wait for the text “Saved” to appear"),
        ({"action": "expect_visible", "locator": {"test_id": "toast"}},
         "Check that the element with test ID “toast” is visible"),
        ({"action": "expect_value", "locator": {"label": "Qty"}, "value": "2"},
         "Check that the field labelled “Qty” holds “2”"),
        ({"action": "expect_url", "path": "/done"}, "Check that the page's address is /done"),
        ({"action": "click", "locator": {"role": "button"}}, "Click a button"),
    ],
)  # fmt: skip
def test_every_action_has_a_sentence(step: dict[str, Any], words: str) -> None:
    assert step_words(UiStep.model_validate(step)) == words


# --- validator -----------------------------------------------------------------------------------


def test_a_ui_test_starts_by_opening_a_page() -> None:
    document = f05()
    document["steps"] = document["steps"][1:]

    assert check(document) == ["a UI test starts with a goto step that opens a page"]


def test_a_ui_test_acts_on_the_page() -> None:
    document = f05()
    document["steps"] = [document["steps"][0], document["steps"][3]]

    assert check(document) == [
        "a UI test does something on the page: a fill, click, select or press step"
    ]


def test_a_ui_test_ends_with_a_check() -> None:
    document = f05()
    document["steps"].append({"action": "click", "locator": {"role": "button", "name": "Add"}})

    assert check(document) == [
        "a UI test ends by checking the result: its last step is expect_visible, expect_text, "
        "expect_value or expect_url"
    ]


def test_a_ui_test_checks_against_the_case() -> None:
    document = f05()
    document["steps"][3]["text"] = "Sunscreen"

    assert check(document) == [
        "a UI test checks the result against the case: no expect step uses "
        "{{case.input.name}} or {{case.expected.name}}"
    ]


def test_a_check_through_its_locator_counts_as_using_the_case() -> None:
    document = f05()
    document["steps"][3] = {"action": "expect_visible", "locator": {"text": "{{case.input.item}}"}}

    assert check(document) == []


def test_ui_placeholders_resolve_in_every_case() -> None:
    document = f05()
    document["steps"][1]["value"] = "{{case.input.thing}}"
    document["steps"][2]["locator"]["name"] = "{{saved.id}}"

    assert check(document) == [
        'step "type" uses {{case.input.thing}}, but input "thing" is missing from case '
        "p1, f1, f2, f3",
        'step "add" uses {{saved.id}}, which is not a {{case.input.name}} or '
        "{{case.expected.name}} placeholder",
    ]
