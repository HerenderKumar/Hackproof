"""Request and response bodies of the team's M7 routes: claims, tests, snapshots, submissions."""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

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


# --- tests -----------------------------------------------------------------------------------

ClaimState = Literal[
    "drafting", "draft", "approved", "needs_clarification", "ui_pending", "drafting_failed"
]
Drafting = Literal["none", "queued", "running", "done", "failed"]


class TestOut(BaseModel):
    """A drafted test as its team reads it: the steps and the practice cases, never the hidden
    final cases."""

    __test__ = False  # not a pytest class

    id: uuid.UUID
    claim_type: str
    version: int
    spec_hash: str
    steps: list[dict[str, Any]]
    practice_cases: list[dict[str, Any]]
    reason: str | None
    approved_at: datetime | None


class ClaimTest(BaseModel):
    claim_id: str
    text: str
    state: ClaimState
    test: TestOut | None


class TestsView(BaseModel):
    __test__ = False

    claims_version: int | None
    drafting: Drafting
    claims: list[ClaimTest]


class ApproveIn(Strict):
    spec_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


# --- snapshots -------------------------------------------------------------------------------


class SnapshotIn(Strict):
    commit_sha: Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True)] | None = (
        None  # left out: the default branch's head, now
    )


class SnapshotOut(BaseModel):
    id: uuid.UUID
    commit_sha: str
    created_at: datetime


class PendingCommit(BaseModel):
    commit_sha: str
    status: Literal["queued", "running", "refused", "failed"]
    detail: str | None


class SnapshotRequest(BaseModel):
    commit_sha: str
    status: Literal["ready", "queued", "running"]
    snapshot_id: uuid.UUID | None


class SnapshotsView(BaseModel):
    snapshots: list[SnapshotOut]
    pending: list[PendingCommit]


# --- self-checks -----------------------------------------------------------------------------

RequestId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{8,100}$")]


class SelfCheckIn(Strict):
    client_request_id: RequestId  # made once per click, so a resent request finds its job


class SelfCheckStart(BaseModel):
    job_id: uuid.UUID
    status: str


class RunOut(BaseModel):
    build_status: str
    startup_status: str
    startup_detail: str | None


class ResultOut(BaseModel):
    claim_id: str
    case_id: str | None
    verdict: str
    assertion_results: Any
    error: str | None
    duration_ms: int | None


class SelfCheckView(BaseModel):
    job_id: uuid.UUID
    snapshot_id: uuid.UUID | None
    commit_sha: str | None
    status: str
    error: str | None
    run: RunOut | None
    results: list[ResultOut]
    claim_verdicts: dict[str, str]


# --- submissions -----------------------------------------------------------------------------


class ReuseDeclaration(Strict):
    """What the team says it did not write during the event (section 5.2)."""

    starter_code: bool
    earlier_work: bool
    ai_assistance: bool
    notes: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] = ""


class SubmissionIn(Strict):
    snapshot_id: uuid.UUID
    mode: Literal["sandbox"]
    reuse_declaration: ReuseDeclaration
    idempotency_key: RequestId  # made once per page load, so a double click is one submission


class Receipt(BaseModel):
    """The receipt: `receipt_hash` is the SHA-256 of the canonical JSON of the fields above it
    except `team_name`, exactly as they are written here."""

    submission_id: str
    team_id: str
    team_name: str
    commit_sha: str
    archive_sha256: str
    claims_hash: str
    test_spec_hash: str
    rubric_version: int
    submitted_at: str
    receipt_hash: str


class CurrentSubmission(BaseModel):
    receipt: Receipt
    submissions_made: int
    final_run: str  # the final run job's status
