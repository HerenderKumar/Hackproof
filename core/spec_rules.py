"""What a test must do beyond having the right shape (section 6.1; M6 design, part 4).

`ApiSpec` holds a test to the case rules. `problems` checks two more things in code, so that
a drafted test is never weaker than its claim type demands: the steps can run with the cases
(every placeholder resolves, every step asserts its status), and the test meets its claim
type's minimum. Each problem is a short sentence the drafting model can act on.

Pure: it imports only the spec schema, the templates and the hashing helpers, so the runner
image can load it.
"""

from collections.abc import Callable, Iterable, Iterator
from typing import Any

from core.hashing import canonical_json
from core.specs import ApiSpec, ApiStep, Assertion
from core.templating import LOOKUP, PLACEHOLDER

_ALLOWED = range(200, 300)
_REFUSED = (401, 403)


def problems(spec: ApiSpec) -> list[str]:
    """Why `spec` falls short of the rules. Empty when it meets every one."""
    found = [*_placeholder_problems(spec), *_status_problems(spec)]
    by_type = _BY_TYPE.get(spec.claim_type)
    if by_type is not None:
        found.extend(by_type(spec))
    return found


# --- every test ----------------------------------------------------------------------------------


def _placeholder_problems(spec: ApiSpec) -> Iterator[str]:
    cases = [*spec.practice_cases, *spec.final_cases]
    saved: set[str] = set()
    for step in spec.steps:
        used = [*_placeholders(step.request.model_dump())]
        used += [
            name for assertion in step.assertions for name in _placeholders(_fields(assertion))
        ]
        for lookup in dict.fromkeys(used):
            uses = f'step "{step.id}" uses {{{{{lookup}}}}}'
            if not LOOKUP.match(lookup):
                yield (
                    f"{uses}, which is not a {{{{case.input.name}}}}, "
                    "{{case.expected.name}} or {{saved.name}} placeholder"
                )
                continue
            segments = lookup.split(".")
            if segments[0] == "saved":
                if segments[1] not in saved:
                    yield f'{uses}, but no earlier step saves "{segments[1]}"'
                continue
            field, name = segments[1], segments[2]
            missing = [case.id for case in cases if name not in getattr(case, field)]
            if missing:
                yield f'{uses}, but {field} "{name}" is missing from case {", ".join(missing)}'
        saved.update(step.save)


def _status_problems(spec: ApiSpec) -> Iterator[str]:
    for step in spec.steps:
        if not any(assertion.op == "status_equals" for assertion in step.assertions):
            yield f'step "{step.id}" does not assert its status: add a status_equals assertion'


# --- each claim type -----------------------------------------------------------------------------


def _calculation(spec: ApiSpec) -> Iterator[str]:
    if not any(_uses(assertion, "case.") for assertion in _checks(spec.steps)):
        yield (
            "a calculation test checks the result against a case value: no assertion's value "
            "uses {{case.input.name}} or {{case.expected.name}}"
        )


def _create_update(spec: ApiSpec) -> Iterator[str]:
    if len(spec.steps) < 2:
        yield (
            "a create_update test needs at least two steps: one that makes the change and a "
            "later one that reads it back"
        )
    elif not any(True for _ in _checks(spec.steps[1:])):
        yield (
            f'a create_update test reads the change back: no step after "{spec.steps[0].id}" '
            "asserts more than its status"
        )


def _classification(spec: ApiSpec) -> Iterator[str]:
    if len({canonical_json(case.expected) for case in spec.final_cases}) < 2:
        ids = ", ".join(case.id for case in spec.final_cases)
        yield (
            "a classification test's hidden cases expect at least two different results, but "
            f"{ids} all expect the same"
        )
    if not any(_uses(assertion, "case.expected.") for assertion in _checks(spec.steps)):
        yield (
            "a classification test checks the result against the case: no assertion's value "
            "uses {{case.expected.name}}"
        )


def _authentication(spec: ApiSpec) -> Iterator[str]:
    refused = [step for step in spec.steps if any(s in _REFUSED for s in _statuses(step))]
    allowed = [
        step
        for step in spec.steps
        if step not in refused and any(s in _ALLOWED for s in _statuses(step))
    ]
    if not allowed:
        yield "an authentication test needs a step that expects a 2xx status (access allowed)"
    if not refused:
        yield "an authentication test needs a step that expects 401 or 403 (access refused)"


_BY_TYPE: dict[str, Callable[[ApiSpec], Iterable[str]]] = {
    "calculation": _calculation,
    "create_update": _create_update,
    "classification": _classification,
    "authentication": _authentication,
}


# --- reading a step ------------------------------------------------------------------------------


def _statuses(step: ApiStep) -> list[int]:
    """The status codes the step's assertions spell out (a status taken from a case is not one)."""
    return [
        assertion.value
        for assertion in step.assertions
        if assertion.op == "status_equals"
        and isinstance(assertion.value, int)
        and not isinstance(assertion.value, bool)
    ]


def _checks(steps: Iterable[ApiStep]) -> Iterator[Assertion]:
    """The assertions that look at more than the status."""
    for step in steps:
        for assertion in step.assertions:
            if assertion.op != "status_equals":
                yield assertion


def _uses(assertion: Assertion, prefix: str) -> bool:
    return any(name.startswith(prefix) for name in _placeholders(assertion.value))


def _fields(assertion: Assertion) -> list[Any]:
    return [assertion.path, assertion.value, assertion.tolerance, assertion.header]


def _placeholders(value: Any) -> Iterator[str]:
    """Every placeholder's lookup in `value`: strings, lists and dict values, recursively."""
    if isinstance(value, str):
        for match in PLACEHOLDER.finditer(value):
            yield match.group(1).strip()
    elif isinstance(value, list):
        for item in value:
            yield from _placeholders(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _placeholders(item)
