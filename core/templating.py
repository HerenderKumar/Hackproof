"""Templates in specs (section 14.1): `{{case.input.x}}`, `{{case.expected.x}}`, `{{saved.x}}`.

Lookups are dotted names only (a numeric segment indexes a list): no expressions, no `eval`.
A string that is exactly one placeholder becomes the value with its own type, so a number
stays a number; a placeholder inside longer text becomes the value's text form.

Copied into the runner image: standard library only.
"""

import json
import re
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

PLACEHOLDER = re.compile(r"\{\{([^{}]*)\}\}")  # no braces inside, so two never merge
LOOKUP = re.compile(r"^(?:case\.(?:input|expected)|saved)(?:\.[A-Za-z0-9_-]+)+$")


class TemplateError(Exception):
    """The spec refers to something its case or the saved values do not have."""


def resolve(value: Any, context: Mapping[str, Any]) -> Any:
    """Fill every placeholder in `value` (strings, lists and dicts, recursively)."""
    if isinstance(value, str):
        whole = PLACEHOLDER.fullmatch(value)
        if whole is not None:
            return _lookup(whole.group(1), context)
        return _substitute(value, context, encode=None)
    if isinstance(value, list):
        return [resolve(item, context) for item in value]
    if isinstance(value, dict):
        return {key: resolve(item, context) for key, item in value.items()}
    return value


def resolve_text(
    template: str, context: Mapping[str, Any], *, encode: Callable[[str], str] | None = None
) -> str:
    """Fill `template` as text. `encode` applies to each substituted value only."""
    return _substitute(template, context, encode=encode)


def text_form(value: Any) -> str:
    """How a value reads inside text: JSON spelling, numbers without needless zeros."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return text_form(Decimal(repr(value)))
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return str(int(value))
        return format(value.normalize(), "f")
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=_plain_number)


def _substitute(
    template: str, context: Mapping[str, Any], *, encode: Callable[[str], str] | None
) -> str:
    def fill(match: re.Match[str]) -> str:
        text = text_form(_lookup(match.group(1), context))
        return encode(text) if encode is not None else text

    return PLACEHOLDER.sub(fill, template)


def _lookup(inner: str, context: Mapping[str, Any]) -> Any:
    path = inner.strip()
    if not LOOKUP.match(path):
        raise TemplateError(f"{{{{{inner}}}}} is not a case value or a saved value")
    value: Any = context
    for segment in path.split("."):
        if isinstance(value, Mapping) and segment in value:
            value = value[segment]
        elif isinstance(value, list) and segment.isdigit() and int(segment) < len(value):
            value = value[int(segment)]
        else:
            raise TemplateError(f"{{{{{path}}}}}: there is no such value")
    return value


def _plain_number(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"{type(value).__name__} cannot appear in a template value")
