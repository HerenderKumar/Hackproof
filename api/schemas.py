"""Request and response bodies."""

import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from core.db.models import SCAN_EXTENSIONS

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]

# The kickoff scan's extensions (M9): never one whose files are usually secrets or data.
SECRET_EXTENSIONS = frozenset(
    {".env", ".pem", ".key", ".p12", ".pfx", ".crt", ".cer", ".kdbx", ".db", ".sqlite"}
)
_EXTENSION = re.compile(r"^\.[a-z0-9+_-]{1,10}$")


def check_scan_extensions(values: list[str]) -> list[str]:
    """1 to 40 distinct extensions, lower-cased, none of SECRET_EXTENSIONS."""
    cleaned = [value.strip().lower() for value in values]
    if not 1 <= len(cleaned) <= 40:
        raise ValueError("scan_extensions must list 1 to 40 extensions")
    for ext in cleaned:
        if not _EXTENSION.match(ext):
            raise ValueError(
                f"{ext!r} is not an extension like .py: a dot, then 1 to 10 of a-z 0-9 + _ -"
            )
        if ext in SECRET_EXTENSIONS:
            raise ValueError(
                f"{ext} files usually hold secrets or data, so a scan never reads them"
            )
    if len(set(cleaned)) != len(cleaned):
        raise ValueError("scan_extensions must be distinct")
    return cleaned


class Strict(BaseModel):
    """A request body: unknown fields are an error, not silently dropped."""

    model_config = ConfigDict(extra="forbid")


# --- events ----------------------------------------------------------------------------------


class Criterion(Strict):
    id: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$")]
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    description: Annotated[str, StringConstraints(max_length=500)] = ""
    max: int = Field(ge=1, le=100)


class EventIn(Strict):
    name: Name
    kickoff_at: AwareDatetime
    deadline_at: AwareDatetime
    rubric: list[Criterion] = Field(min_length=1, max_length=20)
    policy_text: Annotated[str, StringConstraints(max_length=20_000)] = ""
    bulk_push_lines: int = Field(default=2000, ge=1)
    bulk_push_window_minutes: int = Field(default=60, ge=1, le=1440)
    poll_interval_seconds: int = Field(default=300, ge=30, le=3600)
    # When teams' claims freeze. Left out, it falls two thirds of the way through the event.
    claims_lock_at: AwareDatetime | None = None
    # The kickoff scan (M9).
    scan_extensions: list[str] = Field(default_factory=lambda: list(SCAN_EXTENSIONS))
    common_code_min_teams: int = Field(default=3, ge=2)
    baseline_retention_days: int = Field(default=30, ge=1, le=365)

    @field_validator("scan_extensions")
    @classmethod
    def _extensions(cls, values: list[str]) -> list[str]:
        return check_scan_extensions(values)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.deadline_at <= self.kickoff_at:
            raise ValueError("deadline_at must be after kickoff_at")
        lock = self.claims_lock_at
        if lock is not None and not self.kickoff_at <= lock <= self.deadline_at:
            raise ValueError("claims_lock_at must lie between kickoff_at and deadline_at")
        ids = [criterion.id for criterion in self.rubric]
        if len(set(ids)) != len(ids):
            raise ValueError("rubric criteria must have distinct ids")
        return self


class EventPatch(Strict):
    name: Name | None = None
    kickoff_at: AwareDatetime | None = None
    deadline_at: AwareDatetime | None = None
    rubric: list[Criterion] | None = None
    policy_text: str | None = None
    bulk_push_lines: int | None = None
    bulk_push_window_minutes: int | None = None
    poll_interval_seconds: int | None = None
    claims_lock_at: AwareDatetime | None = None
    scan_extensions: list[str] | None = None
    common_code_min_teams: int | None = None
    baseline_retention_days: int | None = None


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    kickoff_at: datetime
    deadline_at: datetime
    claims_lock_at: datetime  # the lock in effect: the event's own, or the default
    rubric_version: int
    rubric: list[dict[str, Any]]
    policy_text: str
    bulk_push_lines: int
    bulk_push_window_minutes: int
    poll_interval_seconds: int
    scores_locked: bool
    created_at: datetime
    scan_extensions: list[str]
    common_code_min_teams: int
    baseline_retention_days: int


# --- teams, judges, links --------------------------------------------------------------------


class ImportedTeam(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    contact_email: str | None
    repo_url: str | None


class SkippedRow(BaseModel):
    line: int
    team_name: str
    reason: str


class ImportResult(BaseModel):
    created: list[ImportedTeam]
    skipped: list[SkippedRow]


class JudgeIn(Strict):
    name: Name
    email: Annotated[str, StringConstraints(strip_whitespace=True, max_length=254)] | None = None


class JudgeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_id: uuid.UUID
    name: str
    email: str | None


class LinkIn(Strict):
    role: Literal["judge", "team"]
    subject_id: uuid.UUID
    expires_in_days: int | None = Field(default=None, ge=1, le=30)


class LinkOut(BaseModel):
    """The only time the URL exists outside the invited person's hands."""

    id: uuid.UUID
    role: Literal["judge", "team"]
    subject_id: uuid.UUID
    url: str
    expires_at: datetime


# --- sign-in ---------------------------------------------------------------------------------


class PasswordIn(Strict):
    password: Annotated[str, StringConstraints(max_length=1000)]


class Me(BaseModel):
    role: Literal["organiser", "judge", "team"]
    subject_id: uuid.UUID | None
    event_id: uuid.UUID | None
    name: str
    expires_at: datetime


# --- the team's own page ---------------------------------------------------------------------

# The runner requests BASE_URL + health_path. Anything but a plain absolute path could move
# that request to another host ("//host", "@host"), so nothing else is accepted.
_HEALTH_PATH = re.compile(r"^/(?!/)[A-Za-z0-9._~!$&'()*+,;=:@%/?-]{0,198}$")


class RepoIn(Strict):
    repo_url: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
    health_path: str | None = None

    @field_validator("health_path")
    @classmethod
    def _plain_path(cls, value: str | None) -> str | None:
        if value is not None and not _HEALTH_PATH.match(value):
            raise ValueError("health_path must be a path that starts with one '/'")
        return value


class TeamEvent(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    kickoff_at: datetime
    claims_lock_at: datetime  # the lock in effect: the event's own, or the default
    deadline_at: datetime
    policy_text: str


class TeamRepo(BaseModel):
    url: str
    provider: str
    default_branch: str
    registered_at: datetime
    late: bool  # registered after kickoff; shown to judges as context (section 6.6)


class TeamProgress(BaseModel):
    """How far the team has come, for the overview's next step."""

    has_claims: bool
    tests_approved: int
    tests_total: int  # the current claims version's tests a team can approve: draft or approved
    has_snapshot: bool
    has_submitted: bool


class ModelDoorTerms(BaseModel):
    """What a team's app may use during HackProof's runs (Prune design, B4; D-063)."""

    models: list[str]
    run_budget_usd: float
    max_calls: int
    outside_hosts: list[str]


class TeamView(BaseModel):
    id: uuid.UUID
    name: str
    now: datetime  # the API's clock, so a page's countdown does not trust the browser's
    event: TeamEvent
    rules_acknowledged_at: datetime | None
    repo: TeamRepo | None
    suggested_repo_url: str | None
    health_path: str
    progress: TeamProgress
    model_door: ModelDoorTerms | None = None  # None: the app has no way out during runs
