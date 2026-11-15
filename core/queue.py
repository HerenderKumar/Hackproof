"""The job queue: a Postgres table claimed with FOR UPDATE SKIP LOCKED (section 11, D-004).

Every function takes the caller's session and leaves the commit to the caller, so an enqueue
can share a transaction with the rows it belongs to (a submission and its three jobs).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Table, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import models

Lane = Literal["watch", "heavy"]
ErrorClass = Literal["infra", "app", "budget", "validation"]
RetryOutcome = Literal["retried", "failed", "stale"]


@dataclass(frozen=True)
class Kind:
    lane: Lane
    priority: int
    lease_s: int


# Section 11.1. A lease must outlast the worst case for its kind: a run is at most
# BUILD_TIMEOUT_S + RUN_TIMEOUT_S = 18 minutes. Raise the lease with any timeout you raise.
KINDS: dict[str, Kind] = {
    "kickoff_snapshot": Kind("watch", 90, 120),
    "poll_repo": Kind("watch", 80, 60),
    "prepare_snapshot": Kind("heavy", 60, 300),
    "draft_specs": Kind("heavy", 50, 300),
    "self_check_run": Kind("heavy", 10, 1200),
    "final_run": Kind("heavy", 100, 1200),
    "audit": Kind("heavy", 100, 300),
    "brief": Kind("heavy", 100, 180),
}

# Delay before the retry that follows the first, second and any later infrastructure error.
BACKOFF_S = (30, 120, 300)


class Job(BaseModel):
    """A claimed job. `attempt_id` identifies this claim and guards every write it makes."""

    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: uuid.UUID
    kind: str
    lane: Lane
    priority: int
    team_id: uuid.UUID | None
    snapshot_id: uuid.UUID | None
    submission_id: uuid.UUID | None
    depends_on: uuid.UUID | None
    payload: dict[str, Any]
    idempotency_key: str
    attempt_id: uuid.UUID
    attempts: int
    max_attempts: int


def kickoff_key(team_id: uuid.UUID) -> str:
    """One kickoff snapshot per team, ever."""
    return f"kickoff:{team_id}"


def prepare_key(team_id: uuid.UUID, commit_sha: str) -> str:
    """One snapshot job per team and commit."""
    return f"prepare:{team_id}:{commit_sha}"


def draft_key(team_id: uuid.UUID, claims_version: int) -> str:
    """One drafting job per team and claims version (D-047)."""
    return f"draft:{team_id}:{claims_version}"


def self_check_key(snapshot_id: uuid.UUID, client_request_id: str) -> str:
    """One self-check per snapshot and click: a resent request finds the job it made."""
    return f"self_check:{snapshot_id}:{client_request_id}"


def poll_key(team_id: uuid.UUID, at: datetime, poll_interval_s: int) -> str:
    """One poll per team per interval: every enqueue inside a bucket shares a key."""
    return f"poll:{team_id}:{int(at.timestamp()) // poll_interval_s}"


_jobs = cast(Table, models.Job.__table__)


async def enqueue(
    session: AsyncSession,
    kind: str,
    idempotency_key: str,
    *,
    payload: dict[str, Any] | None = None,
    team_id: uuid.UUID | None = None,
    snapshot_id: uuid.UUID | None = None,
    submission_id: uuid.UUID | None = None,
    depends_on: uuid.UUID | None = None,
    run_after: datetime | None = None,
    max_attempts: int = 3,
) -> tuple[uuid.UUID, bool]:
    """Insert a job unless one with this key exists. Returns its ID and whether it is new."""
    spec = KINDS[kind]
    values: dict[str, Any] = {
        "kind": kind,
        "lane": spec.lane,
        "priority": spec.priority,
        "idempotency_key": idempotency_key,
        "payload": payload or {},
        "team_id": team_id,
        "snapshot_id": snapshot_id,
        "submission_id": submission_id,
        "depends_on": depends_on,
        "max_attempts": max_attempts,
    }
    if run_after is not None:
        values["run_after"] = run_after
    inserted = await session.execute(
        insert(_jobs)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(_jobs.c.id)
    )
    job_id: uuid.UUID | None = inserted.scalar_one_or_none()
    if job_id is not None:
        return job_id, True
    existing = await session.execute(
        select(_jobs.c.id).where(_jobs.c.idempotency_key == idempotency_key)
    )
    return existing.scalar_one(), False


# The claim query of section 11.2. The lease depends on the kind, which is only known once a
# row is chosen, so it is a CASE over the constants in KINDS. The RETURNING list is spelled
# out in the model's column order because the result columns are matched by position.
_LEASE_S = "CASE jobs.kind " + " ".join(
    f"WHEN '{name}' THEN {kind.lease_s}" for name, kind in KINDS.items()
)
_CLAIM = text(
    f"""
    WITH next AS (
      SELECT j.id FROM jobs j
      WHERE j.lane = :lane
        AND j.run_after <= now()
        AND (j.status = 'queued' OR (j.status = 'running' AND j.lease_until < now()))
        AND (j.depends_on IS NULL OR EXISTS (
              SELECT 1 FROM jobs d WHERE d.id = j.depends_on AND d.status IN ('done','failed')))
      ORDER BY j.priority DESC, j.created_at
      FOR UPDATE SKIP LOCKED
      LIMIT 1
    )
    UPDATE jobs SET status = 'running',
                    attempt_id = gen_random_uuid(),
                    attempts = attempts + 1,
                    lease_until = now() + make_interval(secs => {_LEASE_S} END)
    FROM next WHERE jobs.id = next.id
    RETURNING {", ".join(f"jobs.{column.name}" for column in _jobs.c)}
    """
).columns(*_jobs.c)

_FAIL_EXHAUSTED = text(
    """
    UPDATE jobs SET status = 'failed', finished_at = now(), lease_until = NULL,
                    error_class = 'infra', error_detail = :detail
    WHERE id = :id AND attempt_id = :attempt_id AND status = 'running'
    """
)


async def claim(session: AsyncSession, lane: Lane) -> Job | None:
    """Claim the next runnable job in a lane, or None if there is none.

    A job whose lease ran out is claimed again with a fresh attempt ID. If that takes it past
    `max_attempts`, it is marked failed instead of being handed out.
    """
    while True:
        row = (await session.execute(_CLAIM, {"lane": lane})).one_or_none()
        if row is None:
            return None
        job = Job.model_validate(row)
        if job.attempts <= job.max_attempts:
            return job
        await session.execute(
            _FAIL_EXHAUSTED,
            {
                "id": job.id,
                "attempt_id": job.attempt_id,
                "detail": f"lease expired on attempt {job.max_attempts} of {job.max_attempts}",
            },
        )


_COMPLETE = text(
    """
    UPDATE jobs SET status = 'done', finished_at = now(),
                    error_class = :error_class, error_detail = :error_detail
    WHERE id = :id AND attempt_id = :attempt_id AND status = 'running'
    RETURNING id
    """
)


async def complete(
    session: AsyncSession,
    job_id: uuid.UUID,
    attempt_id: uuid.UUID,
    *,
    error_class: ErrorClass | None = None,
    error_detail: str | None = None,
) -> bool:
    """Mark a job done. Returns False if this attempt is stale; the caller discards its work.

    `error_class` records a degraded but finished outcome: a budget hit or a validation
    fallback (section 11.3). Application errors are results, so they complete without one.
    """
    updated = await session.execute(
        _COMPLETE,
        {
            "id": job_id,
            "attempt_id": attempt_id,
            "error_class": error_class,
            "error_detail": error_detail,
        },
    )
    return updated.first() is not None


_REQUEUE = text(
    """
    UPDATE jobs SET status = 'queued', lease_until = NULL,
                    run_after = now() + make_interval(secs => :backoff_s),
                    error_class = :error_class, error_detail = :error_detail
    WHERE id = :id AND attempt_id = :attempt_id AND status = 'running'
    RETURNING id
    """
)
_FAIL = text(
    """
    UPDATE jobs SET status = 'failed', finished_at = now(), lease_until = NULL,
                    error_class = :error_class, error_detail = :error_detail
    WHERE id = :id AND attempt_id = :attempt_id AND status = 'running'
    RETURNING id
    """
)


async def retry_or_fail(
    session: AsyncSession, job: Job, error_class: ErrorClass, error_detail: str
) -> RetryOutcome:
    """End an attempt that could not finish.

    Infrastructure errors go back to the queue with backoff while attempts remain. Anything
    else, or an infrastructure error on the last attempt, fails the job. A stale attempt
    changes nothing.
    """
    params: dict[str, Any] = {
        "id": job.id,
        "attempt_id": job.attempt_id,
        "error_class": error_class,
        "error_detail": error_detail,
    }
    if error_class == "infra" and job.attempts < job.max_attempts:
        backoff_s = BACKOFF_S[min(job.attempts, len(BACKOFF_S)) - 1]
        updated = await session.execute(_REQUEUE, {**params, "backoff_s": float(backoff_s)})
        outcome: RetryOutcome = "retried"
    else:
        updated = await session.execute(_FAIL, params)
        outcome = "failed"
    return outcome if updated.first() is not None else "stale"
