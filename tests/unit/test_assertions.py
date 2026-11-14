"""Assertion ops (section 14.1). Results record expected and actual, so a judge can check them."""

# The op table below keeps one case per line.
# ruff: noqa: E501

import json
from decimal import Decimal
from typing import Any

import pytest

from core.assertions import (
    NOT_JSON,
    InvalidPath,
    Response,
    evaluate,
    find,
    parse_body,
    to_json_value,
)
from core.specs import Assertion


def resp(text: str, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    lowered = {name.lower(): value for name, value in (headers or {}).items()}
    return Response(status=status, headers=lowered, text=text, body=parse_body(text))


def check(body: str, status: int = 200, headers: dict[str, str] | None = None, **spec: Any) -> bool:
    return evaluate(Assertion(**spec), resp(body, status, headers)).passed


@pytest.mark.parametrize(
    ("spec", "body", "passes"),
    [
        ({"op": "equals", "path": "$.label", "value": "negative"}, '{"label": "negative"}', True),
        ({"op": "equals", "path": "$.label", "value": "negative"}, '{"label": "positive"}', False),
        ({"op": "equals", "value": {"a": 1}}, '{"a": 1}', True),
        ({"op": "not_equals", "path": "$.label", "value": "positive"}, '{"label": "negative"}', True),
        ({"op": "not_equals", "path": "$.label", "value": "positive"}, '{"label": "positive"}', False),
        ({"op": "approx_equals", "path": "$.m", "value": 26.219, "tolerance": 0.001}, '{"m": 26.2188}', True),
        ({"op": "approx_equals", "path": "$.m", "value": 26.219, "tolerance": 0.001}, '{"m": 26.3}', False),
        ({"op": "contains", "path": "$.t", "value": "ell"}, '{"t": "hello"}', True),
        ({"op": "contains", "path": "$.t", "value": "b"}, '{"t": ["a", "b"]}', True),
        ({"op": "contains", "path": "$.t", "value": "k"}, '{"t": {"k": 1}}', True),
        ({"op": "contains", "path": "$.t", "value": "z"}, '{"t": "hello"}', False),
        ({"op": "exists", "path": "$.id"}, '{"id": 1}', True),
        ({"op": "exists", "path": "$.id"}, "{}", False),
        ({"op": "length_equals", "path": "$.s", "value": 3}, '{"s": [1, 2, 3]}', True),
        ({"op": "length_equals", "path": "$.s", "value": Decimal("3")}, '{"s": [1, 2, 3]}', True),
        ({"op": "length_equals", "path": "$.s", "value": 2}, '{"s": [1, 2, 3]}', False),
        ({"op": "gte", "path": "$.n", "value": 5}, '{"n": 5}', True),
        ({"op": "gte", "path": "$.n", "value": 5}, '{"n": 4.99}', False),
        ({"op": "lte", "path": "$.n", "value": 5}, '{"n": 5.0}', True),
        ({"op": "lte", "path": "$.n", "value": 5}, '{"n": 6}', False),
        ({"op": "matches", "path": "$.k", "value": "^[0-9a-f]{8}$"}, '{"k": "deadbeef"}', True),
        ({"op": "matches", "path": "$.k", "value": "^[0-9a-f]{8}$"}, '{"k": "nope"}', False),
        ({"op": "sum_equals", "path": "$.s[*]", "value": Decimal("100.00")}, '{"s": [33.34, 33.33, 33.33]}', True),
        ({"op": "sum_equals", "path": "$.s[*]", "value": Decimal("100.00")}, '{"s": [33.33, 33.33, 33.33]}', False),
        ({"op": "body_contains", "value": "c,f"}, "c,f\n100,212\n", True),
        ({"op": "body_contains", "value": "100,212"}, "c,f\n", False),
    ],
)  # fmt: skip
def test_each_op_passes_and_fails(spec: dict[str, Any], body: str, passes: bool) -> None:
    assert check(body, **spec) is passes


@pytest.mark.parametrize(("status", "passes"), [(200, True), (201, False)])
def test_status_equals(status: int, passes: bool) -> None:
    assert check("{}", status=status, op="status_equals", value=200) is passes


def test_the_bill_splitter_failure_shows_exact_money() -> None:
    result = evaluate(
        Assertion(op="sum_equals", path="$.shares[*]", value=Decimal("100.00"), tolerance=0),
        resp('{"shares": [33.33, 33.33, 33.33]}'),
    )

    assert result.to_json() == {
        "op": "sum_equals",
        "path": "$.shares[*]",
        "expected": "100.00",
        "actual": "99.99",
        "passed": False,
        "note": None,
        "step": None,
    }


@pytest.mark.parametrize(
    ("body", "value"),
    [
        ('{"f": 212.0}', 212),
        ('{"t": 100.00}', Decimal("100")),
        ('{"x": 98.6}', 98.6),
        ('{"x": 0}', 0.0),
    ],
)
def test_numbers_compare_by_value_not_by_form(body: str, value: Any) -> None:
    key = next(iter(json.loads(body)))
    assert check(body, op="equals", path=f"$.{key}", value=value)


def test_actual_values_are_shown_exactly_as_the_app_wrote_them() -> None:
    result = evaluate(Assertion(op="equals", path="$.price", value=100), resp('{"price": 100.00}'))

    assert result.passed
    assert result.to_json()["actual"] == "100.00"


def test_precision_beyond_a_float_is_not_rounded_away() -> None:
    assert not check('{"x": 1.00000000000000000001}', op="equals", path="$.x", value=1)


def test_float_error_never_reaches_a_sum() -> None:
    assert check('{"v": [0.1, 0.2]}', op="sum_equals", path="$.v[*]", value=0.3)


@pytest.mark.parametrize(
    ("body", "value"),
    [('{"x": "2"}', 2), ('{"x": true}', 1), ('{"x": 1}', True), ('{"x": [1]}', [True])],
)
def test_types_never_mix(body: str, value: Any) -> None:
    assert not check(body, op="equals", path="$.x", value=value)


def test_json_assertions_on_a_non_json_body_fail_with_a_note() -> None:
    page = resp("<html>Server Error</html>", status=500)

    result = evaluate(Assertion(op="equals", path="$.label", value="x"), page)

    assert not result.passed
    assert result.note == "the response is not JSON"
    assert evaluate(Assertion(op="status_equals", value=500), page).passed
    assert evaluate(Assertion(op="body_contains", value="Server Error"), page).passed


@pytest.mark.parametrize("op", ["exists", "equals", "length_equals", "sum_equals"])
def test_nothing_at_the_path_is_a_failure_not_an_error(op: str) -> None:
    result = evaluate(Assertion(op=op, path="$.missing", value=1), resp('{"here": 1}'))  # type: ignore[arg-type]

    assert not result.passed
    assert result.note == "nothing at $.missing"


def test_header_names_are_matched_in_any_case() -> None:
    csv = {"Content-Type": "text/csv; charset=utf-8"}

    assert check("a,b", headers=csv, op="header_contains", header="Content-Type", value="text/csv")
    assert check("a,b", headers=csv, op="header_contains", header="content-type", value="text/csv")
    missing = evaluate(Assertion(op="header_contains", header="X-Nope", value="a"), resp("a"))
    assert not missing.passed and missing.note == "no header X-Nope"


def test_matches_looks_only_at_the_first_16_kb() -> None:
    body = json.dumps({"t": "x" * 20_000 + "NEEDLE"})

    assert not check(body, op="matches", path="$.t", value="NEEDLE")
    assert check(body, op="matches", path="$.t", value="^x+$")


def test_a_non_number_where_a_number_is_needed_fails_with_a_note() -> None:
    result = evaluate(Assertion(op="sum_equals", path="$.v[*]", value=1), resp('{"v": [1, "a"]}'))
    assert not result.passed and result.note == "not a number"

    result = evaluate(Assertion(op="gte", path="$.v", value=1), resp('{"v": "3"}'))
    assert not result.passed and result.note == "not a number"


def test_a_malformed_jsonpath_is_the_specs_fault() -> None:
    with pytest.raises(InvalidPath):
        evaluate(Assertion(op="exists", path="$..[?("), resp("{}"))


def test_find_returns_every_match_and_nothing_for_non_json() -> None:
    assert find("$.s[*]", {"s": [1, 2]}) == [1, 2]
    assert find("$.s", NOT_JSON) == []


def test_json_values_keep_exact_decimals() -> None:
    assert to_json_value({"a": [Decimal("99.990"), 1, "x"], "b": NOT_JSON}) == {
        "a": ["99.990", 1, "x"],
        "b": None,
    }
