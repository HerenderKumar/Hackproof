"""Test specs (sections 6.1 and 14): their shape, the case rules, hashes and modes.

A spec has one set of rules (its steps and assertions) and two sets of inputs: the practice
case the team sees, and the hidden cases only a final run uses (the human, 2026-11-13).

This module is copied into the runner image, so it imports nothing but Pydantic and
`core.hashing`.
"""

from collections.abc import Iterable, Mapping
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
        # The runner appends the path to the app's own address, so a path must not be able to
        # name another host.
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
        if len(self.practice_cases) != PRACTICE_CASES:
            raise ValueError(f"a test has exactly {PRACTICE_CASES} practice case")
        edge_cases = sum(case.boundary for case in self.final_cases)
        if len(self.final_cases) != HIDDEN_CASES or edge_cases != HIDDEN_EDGE_CASES:
            raise ValueError(
                f"a test has exactly {HIDDEN_CASES} hidden cases, "
                f"{HIDDEN_EDGE_CASES} of them edge cases (boundary: true)"
            )
        cases = [*self.practice_cases, *self.final_cases]
        if len({case.id for case in cases}) != len(cases):
            raise ValueError("case IDs must be unique across practice and hidden cases")
        inputs = [canonical_json(case.input) for case in cases]
        if len(set(inputs)) != len(inputs):
            raise ValueError(
                "a hidden case must not repeat the practice input or another hidden input"
            )
        return self


class StartupSpec(BaseModel):
    kind: Literal["startup"]
    claim_type: Literal["startup"]
    steps: list[dict[str, str]]
    practice_cases: list[Any] = []
    final_cases: list[Any] = []


class UiSpec(BaseModel):
    """A UI spec (section 14.2). Its steps are validated when UI tests arrive (M11)."""

    model_config = ConfigDict(extra="allow")

    kind: Literal["ui"]
    claim_id: str
    claim_type: ClaimType


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
