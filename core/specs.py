"""Test specs (sections 6.1 and 14): their shape, the case rules, hashes and modes.

A spec has one set of rules (its steps and assertions) and two sets of inputs: the practice
case the team sees, and the hidden cases only a final run uses (the human, 2026-11-13).

This module is copied into the runner image, so it imports nothing but Pydantic and
`core.hashing`.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from core.hashing import canonical_json, hash_json

Mode = Literal["practice", "final"]
ClaimType = Literal[
    "startup", "calculation", "create_update", "classification", "authentication", "ui_workflow"
]
AssertionOp = Literal[
    "status_equals", "equals", "not_equals", "approx_equals", "contains", "exists",
    "length_equals", "gte", "lte", "matches", "sum_equals", "header_contains", "body_contains",
]  # fmt: skip

PRACTICE_CASES = 1
HIDDEN_CASES = 3
HIDDEN_EDGE_CASES = 2

_HASHED_KEYS = ("kind", "claim_type", "steps", "practice_cases", "final_cases")


class Case(BaseModel):
    id: str
    input: dict[str, Any] = {}
    expected: dict[str, Any] = {}
    boundary: bool = False


class Request(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str
    query: dict[str, Any] = {}
    headers: dict[str, str] = {}
    json_body: Any = Field(default=None, alias="json")

    @field_validator("path")
    @classmethod
    def _a_route_on_the_app(cls, path: str) -> str:
        return route_path(path)


def route_path(path: str) -> str:
    """`path` if it names a route on the app. The runner appends a path to the app's own
    address, so a path must not be able to name another host."""
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        raise ValueError("a path starts with a single / and names a route on the app")
    return path


class Assertion(BaseModel):
    op: AssertionOp
    path: str | None = None
    value: Any = None
    tolerance: Any = 0
    header: str | None = None


class ApiStep(BaseModel):
    id: str
    request: Request
    save: dict[str, str] = {}
    assertions: list[Assertion] = []


class ApiSpecShape(BaseModel):
    """An API spec's structure. The runner parses this: a run carries one mode's cases only."""

    kind: Literal["api"]
    claim_id: str
    claim_type: ClaimType
    steps: list[ApiStep] = Field(min_length=1)
    practice_cases: list[Case] = []
    final_cases: list[Case] = []


class ApiSpec(ApiSpecShape):
    """A complete API spec, as stored and approved: the structure plus the case rules."""

    @model_validator(mode="after")
    def _case_rules(self) -> "ApiSpec":
        check_cases(self.practice_cases, self.final_cases)
        return self


def check_cases(practice: Sequence[Case], final: Sequence[Case]) -> None:
    """The case rules every behaviour test obeys (D-047). Raises `ValueError`."""
    if len(practice) != PRACTICE_CASES:
        raise ValueError(f"a test has exactly {PRACTICE_CASES} practice case")
    edge_cases = sum(case.boundary for case in final)
    if len(final) != HIDDEN_CASES or edge_cases != HIDDEN_EDGE_CASES:
        raise ValueError(
            f"a test has exactly {HIDDEN_CASES} hidden cases, "
            f"{HIDDEN_EDGE_CASES} of them edge cases (boundary: true)"
        )
    cases = [*practice, *final]
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("case IDs must be unique across practice and hidden cases")
    inputs = [canonical_json(case.input) for case in cases]
    if len(set(inputs)) != len(inputs):
        raise ValueError("a hidden case must not repeat the practice input or another hidden input")


class StartupSpec(BaseModel):
    kind: Literal["startup"]
    claim_type: Literal["startup"]
    steps: list[dict[str, str]]
    practice_cases: list[Any] = []
    final_cases: list[Any] = []


# --- UI specs (section 14.2, M11 design part 1) ------------------------------------------------

MAX_UI_STEPS = 12

Role = Literal[
    "alert", "article", "banner", "button", "cell", "checkbox", "columnheader", "combobox",
    "complementary", "contentinfo", "dialog", "figure", "form", "grid", "group", "heading", "img",
    "link", "list", "listbox", "listitem", "main", "menu", "menuitem", "navigation", "option",
    "paragraph", "progressbar", "radio", "region", "row", "search", "searchbox", "slider",
    "spinbutton", "status", "switch", "tab", "table", "tablist", "tabpanel", "textbox",
]  # fmt: skip
UiAction = Literal[
    "goto", "fill", "click", "select", "press", "wait_for",
    "expect_visible", "expect_text", "expect_value", "expect_url",
]  # fmt: skip
ACTING: tuple[UiAction, ...] = ("fill", "click", "select", "press")
EXPECTING: tuple[UiAction, ...] = ("expect_visible", "expect_text", "expect_value", "expect_url")
NAMED_KEYS = (
    "Enter", "Tab", "Escape", "Backspace", "Delete", "Space", "Home", "End", "PageUp",
    "PageDown", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
)  # fmt: skip
LOCATOR_KINDS = ("role", "label", "placeholder", "text", "test_id")

# What each action needs ("locator?" is optional) and nothing else it may carry.
_ACTION_FIELDS: dict[str, tuple[str, ...]] = {
    "goto": ("path",),
    "fill": ("locator", "value"),
    "click": ("locator",),
    "select": ("locator", "value"),
    "press": ("key", "locator?"),
    "wait_for": ("locator",),
    "expect_visible": ("locator",),
    "expect_text": ("locator", "text"),
    "expect_value": ("locator", "value"),
    "expect_url": ("path",),
}
_STEP_FIELDS = ("path", "locator", "value", "text", "key")
_STEP_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


class Locator(BaseModel):
    """How a step finds one element, in Playwright's order of preference (section 14.2)."""

    model_config = ConfigDict(extra="forbid")

    role: Role | None = None
    name: str | None = None
    label: str | None = None
    placeholder: str | None = None
    text: str | None = None
    test_id: str | None = None
    exact: bool = False

    @model_validator(mode="after")
    def _one_way_to_find(self) -> "Locator":
        given = [kind for kind in LOCATOR_KINDS if getattr(self, kind) is not None]
        if len(given) != 1:
            raise ValueError(
                "a locator has exactly one of role (with an optional name), label, "
                "placeholder, text or test_id"
            )
        if self.name is not None and self.role is None:
            raise ValueError("a locator's name goes with a role")
        for kind in (*LOCATOR_KINDS[1:], "name"):
            if getattr(self, kind) == "":
                raise ValueError(f"a locator's {kind} is not empty")
        return self

    def kind(self) -> str:
        return next(kind for kind in LOCATOR_KINDS if getattr(self, kind) is not None)


class UiStep(BaseModel):
    """One action on the page. Strings may hold `{{case.input.x}}` and `{{case.expected.x}}`."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    action: UiAction
    path: str | None = None
    locator: Locator | None = None
    value: str | None = None
    text: str | None = None
    key: str | None = None

    @model_validator(mode="after")
    def _fields_of_its_action(self) -> "UiStep":
        wanted = _ACTION_FIELDS[self.action]
        required = [name for name in wanted if not name.endswith("?")]
        allowed = {name.rstrip("?") for name in wanted}
        missing = [name for name in required if getattr(self, name) is None]
        extra = [name for name in _STEP_FIELDS if name not in allowed and getattr(self, name)]
        if missing or extra:
            needs = " and ".join(required)
            optional = [name.rstrip("?") for name in wanted if name.endswith("?")]
            more = f" (and optionally {', '.join(optional)})" if optional else ""
            raise ValueError(f"a {self.action} step has {needs}{more} and nothing else")
        if self.path is not None:
            route_path(self.path)
        if self.key is not None and self.key not in NAMED_KEYS and len(self.key) != 1:
            raise ValueError(f"key is one character or one of {', '.join(NAMED_KEYS)}")
        if self.id is not None and not _STEP_ID.match(self.id):
            raise ValueError("a step id is up to 40 letters, digits, - or _")
        return self


class UiSpecShape(BaseModel):
    """A UI spec's structure. The runner parses this: a run carries one mode's cases only.
    Steps without an id get `s1`, `s2`, ... by position."""

    kind: Literal["ui"]
    claim_id: str
    claim_type: Literal["ui_workflow"]
    steps: list[UiStep] = Field(min_length=1, max_length=MAX_UI_STEPS)
    practice_cases: list[Case] = []
    final_cases: list[Case] = []

    @model_validator(mode="after")
    def _step_ids(self) -> "UiSpecShape":
        for number, step in enumerate(self.steps, start=1):
            if step.id is None:
                step.id = f"s{number}"
        ids = [step.id for step in self.steps]
        if len(set(ids)) != len(ids):
            raise ValueError("step ids are unique in a test")
        return self


class UiSpec(UiSpecShape):
    """A complete UI spec, as stored and approved: the structure plus the case rules."""

    @model_validator(mode="after")
    def _case_rules(self) -> "UiSpec":
        check_cases(self.practice_cases, self.final_cases)
        return self


_ANY_SPEC: TypeAdapter[ApiSpec | StartupSpec | UiSpec] = TypeAdapter(
    Annotated[ApiSpec | StartupSpec | UiSpec, Field(discriminator="kind")]
)


def parse_spec(data: Mapping[str, Any]) -> ApiSpec | StartupSpec | UiSpec:
    """Validate a complete spec of any kind. Raises `pydantic.ValidationError`."""
    return _ANY_SPEC.validate_python(dict(data))


def spec_hash(spec: Mapping[str, Any]) -> str:
    """SHA-256 of the spec's rules and cases (section 6.1): what an approval pins."""
    return hash_json({key: spec.get(key) for key in _HASHED_KEYS})


def test_spec_hash(spec_hashes: Iterable[str]) -> str:
    """SHA-256 of the sorted approved spec hashes, stored on a submission (section 6.1)."""
    return hash_json(sorted(spec_hashes))


test_spec_hash.__test__ = False  # type: ignore[attr-defined]  # not a pytest test


def for_mode(spec: Mapping[str, Any], mode: Mode) -> dict[str, Any]:
    """A copy of `spec` carrying only `mode`'s cases, so a run never holds the other set."""
    other = "final_cases" if mode == "practice" else "practice_cases"
    return {**spec, other: []}
