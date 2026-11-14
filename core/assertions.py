"""Assertion ops (section 14.1): deterministic checks of one HTTP response.

Every result records what was expected and what the app returned, so a judge can check a
failure instead of trusting it. Numbers compare as `Decimal` built from the JSON text, so
money never suffers float error; a bool never equals a number and a number never equals its
string. Nothing found where a spec looks is the app's failure; a spec that cannot be evaluated
(a malformed JSONPath or pattern, a number op without a number) raises `InvalidAssertion`.

Copied into the runner image: standard library, jsonpath-ng and `core` only.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from typing import Any, Final

from jsonpath_ng.parser import parse as parse_jsonpath

from core.specs import Assertion
from core.templating import text_form

MATCH_TEXT_LIMIT = 16_384
_BODY_EVIDENCE = 500  # characters of body text kept as a body assertion's actual value


class _NotJson:
    def __repr__(self) -> str:
        return "NOT_JSON"


NOT_JSON: Final = _NotJson()


class InvalidAssertion(Exception):
    """The spec's assertion cannot be evaluated: the spec's fault, never the app's."""


class InvalidPath(InvalidAssertion):
    """The spec's JSONPath does not parse."""


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]  # names in lower case
    text: str
    body: Any  # the parsed JSON, or NOT_JSON


@dataclass(frozen=True)
class AssertionResult:
    op: str
    path: str | None
    expected: Any
    actual: Any
    passed: bool
    note: str | None = None
    step: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "path": self.path,
            "expected": to_json_value(self.expected),
            "actual": to_json_value(self.actual),
            "passed": self.passed,
            "note": self.note,
            "step": self.step,
        }


def parse_body(text: str) -> Any:
    try:
        return json.loads(text, parse_float=Decimal)
    except ValueError:
        return NOT_JSON


def find(path: str, body: Any) -> list[Any]:
    """Every value at `path` in `body`; nothing for a body that is not JSON."""
    if body is NOT_JSON:
        return []
    return [match.value for match in _compile(path).find(body)]


def to_json_value(value: Any) -> Any:
    """A JSON-safe copy: `Decimal` as its exact text, `NOT_JSON` as null."""
    if isinstance(value, Decimal):
        return str(value)
    if value is NOT_JSON:
        return None
    if isinstance(value, dict):
        return {key: to_json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [to_json_value(item) for item in value]
    return value


def evaluate(assertion: Assertion, response: Response) -> AssertionResult:
    op, path, value = assertion.op, assertion.path, assertion.value

    def result(actual: Any, passed: bool, note: str | None = None) -> AssertionResult:
        return AssertionResult(op, path, value, actual, passed, note)

    if op == "status_equals":
        return result(response.status, _same(response.status, value))
    if op == "body_contains":
        return result(response.text[:_BODY_EVIDENCE], str(value) in response.text)
    if op == "header_contains":
        name = assertion.header or ""
        header = response.headers.get(name.lower())
        if header is None:
            return result(None, False, f"no header {name}")
        return result(header, str(value) in header)

    if response.body is NOT_JSON:
        return result(None, False, "the response is not JSON")
    matches = find(path, response.body) if path else [response.body]
    if op == "exists":
        return result(matches[0] if matches else None, bool(matches), _nothing(path, matches))
    if not matches:
        return result(None, False, _nothing(path, matches))
    actual = matches[0]

    if op == "equals":
        return result(actual, _same(actual, value))
    if op == "not_equals":
        return result(actual, not _same(actual, value))
    if op == "contains":
        return result(actual, _contains(actual, value))
    if op == "length_equals":
        if not isinstance(actual, str | list | dict):
            return result(actual, False, "has no length")
        return result(len(actual), _same(len(actual), value))
    if op == "matches":
        text = (actual if isinstance(actual, str) else text_form(actual))[:MATCH_TEXT_LIMIT]
        return result(actual, _search(str(value), text))
    if op == "sum_equals":
        numbers = [_number(match) for match in matches]
        if any(number is None for number in numbers):
            return result(matches, False, "not a number")
        total = sum((n for n in numbers if n is not None), Decimal(0))
        return result(total, abs(total - _required_number(value)) <= _tolerance(assertion))

    number = _number(actual)
    if number is None:
        return result(actual, False, "not a number")
    if op == "approx_equals":
        return result(actual, abs(number - _required_number(value)) <= _tolerance(assertion))
    if op == "gte":
        return result(actual, number >= _required_number(value))
    return result(actual, number <= _required_number(value))  # lte


# --- helpers -------------------------------------------------------------------------------------


@lru_cache(maxsize=256)
def _compile(path: str) -> Any:
    try:
        return parse_jsonpath(path)
    except Exception as exc:  # jsonpath-ng raises several unrelated types
        raise InvalidPath(f"{path} is not a valid JSONPath: {exc}") from None


def _nothing(path: str | None, matches: list[Any]) -> str | None:
    return None if matches else f"nothing at {path}"


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | Decimal):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value))
    return None


def _required_number(value: Any) -> Decimal:
    number = _number(value)
    if number is None:
        raise InvalidAssertion(f"{value!r} is not a number")
    return number


def _tolerance(assertion: Assertion) -> Decimal:
    return _required_number(assertion.tolerance if assertion.tolerance is not None else 0)


def _same(actual: Any, expected: Any) -> bool:
    actual_number, expected_number = _number(actual), _number(expected)
    if actual_number is not None or expected_number is not None:
        return actual_number is not None and actual_number == expected_number
    if isinstance(actual, list) and isinstance(expected, list):
        return len(actual) == len(expected) and all(map(_same, actual, expected))
    if isinstance(actual, dict) and isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            _same(actual[k], expected[k]) for k in actual
        )
    return type(actual) is type(expected) and bool(actual == expected)


def _contains(actual: Any, value: Any) -> bool:
    if isinstance(actual, str):
        return isinstance(value, str) and value in actual
    if isinstance(actual, list):
        return any(_same(item, value) for item in actual)
    if isinstance(actual, dict):
        return isinstance(value, str) and value in actual
    return False


def _search(pattern: str, text: str) -> bool:
    try:
        return re.search(pattern, text) is not None
    except re.error as exc:
        raise InvalidAssertion(f"{pattern!r} is not a valid pattern: {exc}") from None
