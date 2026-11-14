"""Templates (section 14.1): dotted lookups only, no expressions, types kept where they can be."""

import urllib.parse
from decimal import Decimal
from typing import Any

import pytest

from core.templating import TemplateError, resolve, resolve_text, text_form

CTX: dict[str, Any] = {
    "case": {
        "input": {"total": Decimal("100.00"), "people": 3, "tags": ["a"]},
        "expected": {"f": Decimal("212.0")},
    },
    "saved": {"id": 7, "token": "abc", "items": [{"id": 9}], "odd": "a b/c&d?e"},
}


def test_a_whole_placeholder_keeps_its_type() -> None:
    assert resolve("{{case.input.people}}", CTX) == 3
    assert resolve("{{case.input.total}}", CTX) == Decimal("100.00")
    assert resolve("{{case.input.tags}}", CTX) == ["a"]


def test_a_placeholder_inside_text_becomes_text() -> None:
    assert resolve("/items/{{saved.id}}", CTX) == "/items/7"
    assert resolve("Bearer {{saved.token}}", CTX) == "Bearer abc"
    assert resolve("{{case.input.total}},{{case.expected.f}}", CTX) == "100,212"


def test_structures_are_resolved_all_the_way_down() -> None:
    value = {"total": "{{case.input.total}}", "x": ["{{saved.id}}", 5]}

    assert resolve(value, CTX) == {"total": Decimal("100.00"), "x": [7, 5]}


def test_spaces_inside_the_braces_are_allowed() -> None:
    assert resolve("{{ saved.id }}", CTX) == 7


def test_a_numeric_segment_indexes_a_list() -> None:
    assert resolve("{{saved.items.0.id}}", CTX) == 9


@pytest.mark.parametrize(
    "template",
    [
        "{{env.HOME}}",
        "{{case.input.nope}}",
        "{{saved.items.5}}",
        "{{case.input.people + 1}}",
        "{{}}",
        "{{case}}",
        "{{case.secret.x}}",
        "x {{saved.missing}} y",
    ],
)
def test_anything_but_a_known_dotted_lookup_is_an_error(template: str) -> None:
    with pytest.raises(TemplateError):
        resolve(template, CTX)


def test_text_without_placeholders_and_non_strings_are_unchanged() -> None:
    assert resolve("plain", CTX) == "plain"
    assert resolve(4, CTX) == 4
    assert resolve(None, CTX) is None


def test_resolve_text_encodes_only_the_substituted_values() -> None:
    def quote(text: str) -> str:
        return urllib.parse.quote(text, safe="")

    assert resolve_text("/items/{{saved.odd}}", CTX, encode=quote) == "/items/a%20b%2Fc%26d%3Fe"
    assert resolve_text("/items/{{saved.id}}", CTX) == "/items/7"


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (True, "true"),
        (False, "false"),
        (None, "null"),
        (Decimal("98.60"), "98.6"),
        (Decimal("212.0"), "212"),
        (Decimal("100.00"), "100"),
        (Decimal("0.01"), "0.01"),
        (2.5, "2.5"),
        (-40, "-40"),
        ("as is", "as is"),
        ({"a": [1, 2]}, '{"a":[1,2]}'),
    ],
)
def test_text_form(value: Any, text: str) -> None:
    assert text_form(value) == text
