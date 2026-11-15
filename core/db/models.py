"""SQLAlchemy models for the tables of section 10.

The migrations own the DDL (D-028): constraints, defaults and the immutability trigger are
defined there. These classes only describe columns, and `tests/unit/test_schema.py` checks
that they match the migrated database.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, LargeBinary, Text, text
from sqlalchemy.dialects.postgresql import JSONB, REAL
from sqlalchemy.orm import DeclarativeBase, Mapped, MappedColumn, mapped_column

# The extensions a kickoff scan reads unless the organiser changes them (M9, migration 0004).
SCAN_EXTENSIONS = (
    ".js", ".jsx", ".ts", ".tsx", ".py", ".c", ".h", ".cpp", ".cc", ".hpp", ".java", ".go",
)  # fmt: skip


def json_list(items: tuple[str, ...]) -> str:
    return "[" + ",".join(f'"{item}"' for item in items) + "]"


class Base(DeclarativeBase):
    type_annotation_map = {str: Text, datetime: DateTime(timezone=True)}


def _uuid_pk() -> MappedColumn[uuid.UUID]:
    return mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))


def _now() -> MappedColumn[datetime]:
    return mapped_column(server_default=text("now()"))


def _json(default: str | None = None) -> MappedColumn[Any]:
    """A NOT NULL JSONB column, optionally with a database default."""
    return mapped_column(
        JSONB, nullable=False, server_default=text(f"'{default}'") if default else None
    )


def _json_or_null() -> MappedColumn[Any]:
    # Python None is stored as SQL NULL, not as the JSON value `null`.
    return mapped_column(JSONB(none_as_null=True), nullable=True)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str]
    kickoff_at: Mapped[datetime]
    deadline_at: Mapped[datetime]
    # When claims freeze; NULL means two thirds of the way through (core.events.claims_lock_at).
    claims_lock_at: Mapped[datetime | None]
    rubric_version: Mapped[int] = mapped_column(server_default=text("1"))
    rubric: Mapped[Any] = _json()
    policy_text: Mapped[str] = mapped_column(server_default=text("''"))
    bulk_push_lines: Mapped[int] = mapped_column(server_default=text("2000"))
    bulk_push_window_minutes: Mapped[int] = mapped_column(server_default=text("60"))
    poll_interval_seconds: Mapped[int] = mapped_column(server_default=text("300"))
    scores_locked: Mapped[bool] = mapped_column(server_default=text("false"))
    created_at: Mapped[datetime] = _now()
    # The kickoff scan (M9): which files a device sends, how many teams make code common, and
    # how long baseline code is kept after the deadline.
    scan_extensions: Mapped[Any] = _json(default=json_list(SCAN_EXTENSIONS))
    common_code_min_teams: Mapped[int] = mapped_column(server_default=text("3"))
    baseline_retention_days: Mapped[int] = mapped_column(server_default=text("30"))


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[uuid.UUID] = _uuid_pk()
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id"))
    name: Mapped[str]
    contact_email: Mapped[str | None]
    repo_url: Mapped[str | None]
    repo_provider: Mapped[str | None]
    repository_id: Mapped[str | None]
    default_branch: Mapped[str | None]
    repo_created_at: Mapped[datetime | None]
    registered_at: Mapped[datetime | None]
    rules_acknowledged_at: Mapped[datetime | None]
    health_path: Mapped[str] = mapped_column(server_default=text("'/'"))
    members: Mapped[int | None]


class Judge(Base):
    __tablename__ = "judges"

    id: Mapped[uuid.UUID] = _uuid_pk()
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id"))
    name: Mapped[str]
    email: Mapped[str | None]


class AccessLink(Base):
    __tablename__ = "access_links"

    id: Mapped[uuid.UUID] = _uuid_pk()
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id"))
    role: Mapped[str]
    subject_id: Mapped[uuid.UUID]
    token_hash: Mapped[str]
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = _now()


class RepoObservation(Base):
    __tablename__ = "repo_observations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id"))
    kind: Mapped[str]
    observed_at: Mapped[datetime]
    head_sha: Mapped[str | None]
    previous_head_sha: Mapped[str | None]
    descends_from_previous: Mapped[bool | None]
    lines_added: Mapped[int | None]
    lines_total: Mapped[int | None]
    files_changed: Mapped[int | None]
    commits: Mapped[Any] = _json(default="[]")


class Claims(Base):
    __tablename__ = "claims"

    id: Mapped[uuid.UUID] = _uuid_pk()
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id"))
    version: Mapped[int]
    items: Mapped[Any] = _json()
    claims_hash: Mapped[str]
    # The team's start text, which tests are drafted from with the items (D-047).
    problem_statement: Mapped[str] = mapped_column(default="", server_default=text("''"))
    explanation: Mapped[str] = mapped_column(default="", server_default=text("''"))
    created_at: Mapped[datetime] = _now()


class Snapshot(Base):
    __tablename__ = "snapshots"

    id: Mapped[uuid.UUID] = _uuid_pk()
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id"))
    commit_sha: Mapped[str]
    archive_sha256: Mapped[str]
    archive_path: Mapped[str]
    commit_history: Mapped[Any] = _json()
    lines_total: Mapped[int | None]
    created_at: Mapped[datetime] = _now()


class TestSpec(Base):
    __tablename__ = "test_specs"
    __test__ = False  # not a pytest class, despite the name

    id: Mapped[uuid.UUID] = _uuid_pk()
    # Tests belong to the team's claims version, drafted before any snapshot exists (D-047).
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id"))
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("snapshots.id"))
    claims_version: Mapped[int]
    version: Mapped[int]
    kind: Mapped[str]
    claim_id: Mapped[str | None]
    claim_type: Mapped[str]
    steps: Mapped[Any] = _json()
    practice_cases: Mapped[Any] = _json(default="[]")
    final_cases: Mapped[Any] = _json(default="[]")
    spec_hash: Mapped[str]
    status: Mapped[str]
    clarification_reason: Mapped[str | None]
    approved_at: Mapped[datetime | None]
    approved_spec_hash: Mapped[str | None]
    created_at: Mapped[datetime] = _now()


class Submission(Base):
    """Immutable once written: a trigger blocks UPDATE and DELETE (invariant 3)."""

    __tablename__ = "submissions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id"))
    snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("snapshots.id"))
    mode: Mapped[str]
    deploy_url: Mapped[str | None]
    claims_version: Mapped[int]
    claims_hash: Mapped[str]
    test_spec_hash: Mapped[str]
    rubric_version: Mapped[int]
    runner_version: Mapped[str]
    railpack_version: Mapped[str]
    reuse_declaration: Mapped[Any] = _json()
    submitted_at: Mapped[datetime] = _now()
    idempotency_key: Mapped[str]
    receipt_hash: Mapped[str]


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[str]
    lane: Mapped[str]
    priority: Mapped[int]
    status: Mapped[str] = mapped_column(server_default=text("'queued'"))
    team_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("teams.id"))
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("snapshots.id"))
    submission_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("submissions.id"))
    depends_on: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id"))
    payload: Mapped[Any] = _json(default="{}")
    idempotency_key: Mapped[str]
    attempt_id: Mapped[uuid.UUID | None]
    attempts: Mapped[int] = mapped_column(server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(server_default=text("3"))
    lease_until: Mapped[datetime | None]
    run_after: Mapped[datetime] = _now()
    error_class: Mapped[str | None]
    error_detail: Mapped[str | None]
    created_at: Mapped[datetime] = _now()
    finished_at: Mapped[datetime | None]


class RunResult(Base):
    __tablename__ = "run_results"

    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"))
    attempt_id: Mapped[uuid.UUID]
    build_status: Mapped[str]
    build_exit_code: Mapped[int | None]
    build_method: Mapped[str | None]
    startup_status: Mapped[str]
    startup_detail: Mapped[str | None]
    image_digest: Mapped[str | None]
    build_config: Mapped[Any] = _json_or_null()
    flags_used: Mapped[Any] = _json(default="{}")
    # With the model door on (Prune design, B3): calls, refusals, models, tokens, cost, outside
    # connections and the run's receipt. NULL when the door was off.
    model_use: Mapped[Any] = _json_or_null()
    build_log_path: Mapped[str | None]
    app_log_path: Mapped[str | None]
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]


class TestResult(Base):
    __tablename__ = "test_results"
    __test__ = False  # not a pytest class, despite the name

    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"))
    attempt_id: Mapped[uuid.UUID]
    spec_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("test_specs.id"))
    case_id: Mapped[str | None]
    verdict: Mapped[str]
    expected: Mapped[Any] = _json_or_null()
    actual: Mapped[Any] = _json_or_null()
    assertion_results: Mapped[Any] = _json_or_null()
    duration_ms: Mapped[int | None]
    video_path: Mapped[str | None]
    trace_path: Mapped[str | None]
    error: Mapped[str | None]


class Audit(Base):
    __tablename__ = "audits"

    id: Mapped[uuid.UUID] = _uuid_pk()
    submission_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("submissions.id"))
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"))
    attempt_id: Mapped[uuid.UUID]
    content: Mapped[Any] = _json()
    citations_dropped: Mapped[int] = mapped_column(server_default=text("0"))
    partial: Mapped[bool] = mapped_column(server_default=text("false"))
    code_map: Mapped[Any] = _json_or_null()
    code_map_status: Mapped[str | None]
    created_at: Mapped[datetime] = _now()


class Brief(Base):
    __tablename__ = "briefs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    submission_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("submissions.id"))
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"))
    attempt_id: Mapped[uuid.UUID]
    content: Mapped[Any] = _json()
    ai_status: Mapped[str]
    model: Mapped[str | None]
    created_at: Mapped[datetime] = _now()


class ClaimMark(Base):
    __tablename__ = "claim_marks"

    judge_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("judges.id"), primary_key=True)
    submission_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("submissions.id"), primary_key=True)
    claim_id: Mapped[str] = mapped_column(primary_key=True)
    mark: Mapped[str]
    updated_at: Mapped[datetime] = _now()


class Score(Base):
    __tablename__ = "scores"

    judge_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("judges.id"), primary_key=True)
    submission_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("submissions.id"), primary_key=True)
    criterion_id: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[int]
    updated_at: Mapped[datetime] = _now()


class BriefView(Base):
    __tablename__ = "brief_views"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    judge_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("judges.id"))
    submission_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("submissions.id"))
    opened_at: Mapped[datetime] = _now()


class LlmCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id"))
    purpose: Mapped[str]
    model: Mapped[str]
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    duration_ms: Mapped[int | None]
    ok: Mapped[bool]
    created_at: Mapped[datetime] = _now()


# --- M9: the kickoff baseline (D-052) -------------------------------------------------------


class BaselineDevice(Base):
    __tablename__ = "baseline_devices"

    id: Mapped[uuid.UUID] = _uuid_pk()
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id"))
    label: Mapped[str]
    code_hash: Mapped[str]  # hash_token(code); the code itself is shown once
    created_at: Mapped[datetime] = _now()


class Baseline(Base):
    """One scan of one device. Its code is on disk, encrypted; this row is its bookkeeping."""

    __tablename__ = "baselines"

    id: Mapped[uuid.UUID] = _uuid_pk()
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("baseline_devices.id"))
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id"))
    status: Mapped[str]
    started_at: Mapped[datetime] = _now()
    sealed_at: Mapped[datetime | None]
    late: Mapped[bool | None]
    complete: Mapped[bool | None]
    incomplete_reasons: Mapped[Any] = _json(default="[]")
    parts: Mapped[Any] = _json(default="[]")
    bytes_received: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    files: Mapped[int | None]
    summary: Mapped[Any] = _json_or_null()
    unreadable: Mapped[Any] = _json(default="[]")
    unreadable_more: Mapped[int] = mapped_column(server_default=text("0"))
    scanner_version: Mapped[str | None]
    platform: Mapped[str | None]
    extensions: Mapped[Any] = _json_or_null()
    index_settings: Mapped[Any] = _json_or_null()
    index_summary: Mapped[Any] = _json_or_null()
    manifest_sha256: Mapped[str | None]
    receipt_hash: Mapped[str | None]
    error_detail: Mapped[str | None]


class Starter(Base):
    """Starter code the organiser approved: its runs are excluded from every comparison."""

    __tablename__ = "starters"

    id: Mapped[uuid.UUID] = _uuid_pk()
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id"))
    name: Mapped[str]
    archive_sha256: Mapped[str]
    files: Mapped[int]
    runs: Mapped[bytes] = mapped_column(LargeBinary)  # pack_runs: sorted distinct 8-byte hashes
    settings: Mapped[Any] = _json()
    created_at: Mapped[datetime] = _now()


class PriorWorkReport(Base):
    __tablename__ = "prior_work_reports"

    id: Mapped[uuid.UUID] = _uuid_pk()
    submission_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("submissions.id"))
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"))
    attempt_id: Mapped[uuid.UUID]
    status: Mapped[str]
    detail: Mapped[str | None]
    totals: Mapped[Any] = _json_or_null()
    files: Mapped[Any] = _json(default="[]")
    settings: Mapped[Any] = _json(default="{}")
    baseline_ids: Mapped[Any] = _json(default="[]")
    purged_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = _now()


class PriorWorkMatch(Base):
    __tablename__ = "prior_work_matches"

    id: Mapped[uuid.UUID] = _uuid_pk()
    report_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("prior_work_reports.id"))
    ordinal: Mapped[int]
    match_key: Mapped[str]
    kind: Mapped[str]
    late: Mapped[bool]
    tokens: Mapped[int]
    score: Mapped[float | None] = mapped_column(REAL)
    final_path: Mapped[str]
    final_start_line: Mapped[int]
    final_end_line: Mapped[int]
    baseline_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("baselines.id"))
    device_label: Mapped[str]
    old_path: Mapped[str]
    old_start_line: Mapped[int]
    old_end_line: Mapped[int]
    also_in: Mapped[int] = mapped_column(server_default=text("0"))
    # Encrypted; NULL once the baseline's retention has passed.
    final_excerpt: Mapped[bytes | None] = mapped_column(LargeBinary)
    old_excerpt: Mapped[bytes | None] = mapped_column(LargeBinary)
    final_excerpt_line: Mapped[int]
    old_excerpt_line: Mapped[int]


class MatchExplanation(Base):
    """A team's word on a match, or on the whole report when `match_key` is empty. Keyed by
    submission and match key, so it survives a rerun of the report."""

    __tablename__ = "match_explanations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    submission_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("submissions.id"))
    match_key: Mapped[str] = mapped_column(default="", server_default=text("''"))
    text: Mapped[str]
    created_at: Mapped[datetime] = _now()
