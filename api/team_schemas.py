"""Request and response bodies of the team's M7 routes: claims, tests, snapshots, submissions."""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from api.schemas import Strict


def _trimmed(min_length: int, max_length: int) -> StringConstraints:
    return StringConstraints(strip_whitespace=True, min_length=min_length, max_length=max_length)


# --- claims ----------------------------------------------------------------------------------


class ClaimItemIn(Strict):
    """One claim as the team writes it. The server gives it its ID (`c1` to `c3`, by position)."""

    text: Annotated[str, _trimmed(1, 500)]
    target: Annotated[str, _trimmed(0, 200)] = ""
    example_input: Annotated[str, _trimmed(0, 1000)] = ""
    expected_behaviour: Annotated[str, _trimmed(0, 1000)] = ""


class ClaimsIn(Strict):
    problem_statement: Annotated[str, _trimmed(1, 2000)]
    explanation: Annotated[str, _trimmed(1, 6000)]
    items: list[ClaimItemIn] = Field(min_length=1, max_length=3)


class ClaimsOut(BaseModel):
    version: int
    problem_statement: str
    explanation: str
    items: list[dict[str, str]]
    claims_hash: str
    created_at: datetime


class ClaimsView(BaseModel):
    current: ClaimsOut | None
    lock_at: datetime
    locked: bool
    versions_left: int
