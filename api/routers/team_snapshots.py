"""A team's snapshots: one commit of its repository, archived (M7 design, part 4).

A team picks its default branch's head or pastes a full SHA. The API only asks: the worker's
`prepare_snapshot` job archives the commit, or refuses it with a reason the team can read.
"""

import uuid
from typing import Literal, cast

from fastapi import APIRouter, Request, Response
from sqlalchemy import select

from api.deps import Db, TeamUser
from api.errors import api_error
from api.team_common import own_team, require_before_deadline
from api.team_schemas import (
    PendingCommit,
    SnapshotIn,
    SnapshotOut,
    SnapshotRequest,
    SnapshotsView,
)
from core import queue
from core.db import models
from core.errors import InfraError
from core.repo import InvalidCommit, ProviderName, RepoInfo, RepoNotFound, RepoProvider
from core.snapshots import check_commit_sha

router = APIRouter(prefix="/api", tags=["team"])

Pending = Literal["queued", "running", "refused", "failed"]


def team_repo(team: models.Team) -> RepoInfo:
    """The registered repository, as the provider takes it."""
    return RepoInfo(
        provider=cast(ProviderName, team.repo_provider or "github"),
        id=team.repository_id or "",
        url=team.repo_url or "",
        default_branch=team.default_branch or "main",
        created_at=team.repo_created_at,
        is_public=True,
    )


def _pending(job: models.Job) -> Pending | None:
    """Where a prepare job stands for its commit; None once it has made the snapshot."""
    if job.status in ("queued", "running"):
        return cast(Pending, job.status)
    if job.status == "done":
        return "refused" if job.error_class is not None else None
    return "failed"  # failed after its retries, or cancelled


async def _snapshot_id(db: Db, team_id: uuid.UUID, sha: str) -> uuid.UUID | None:
    found: uuid.UUID | None = await db.scalar(
        select(models.Snapshot.id).where(
            models.Snapshot.team_id == team_id, models.Snapshot.commit_sha == sha
        )
    )
    return found


@router.get("/team/snapshots")
async def list_snapshots(session: TeamUser, db: Db) -> SnapshotsView:
    team, _ = await own_team(db, session)
    snapshots = list(
        await db.scalars(
            select(models.Snapshot)
            .where(models.Snapshot.team_id == team.id)
            .order_by(models.Snapshot.created_at.desc(), models.Snapshot.id)
        )
    )
    jobs = await db.scalars(
        select(models.Job)
        .where(models.Job.team_id == team.id, models.Job.kind == "prepare_snapshot")
        .order_by(models.Job.created_at.desc(), models.Job.idempotency_key)
    )
    taken = {row.commit_sha for row in snapshots}
    pending = []
    for job in jobs:
        sha = str(job.payload.get("commit_sha", ""))
        status = _pending(job)
        if sha in taken or status is None:
            continue
        detail = job.error_detail if status in ("refused", "failed") else None
        pending.append(PendingCommit(commit_sha=sha, status=status, detail=detail))
    return SnapshotsView(
        snapshots=[
            SnapshotOut(id=row.id, commit_sha=row.commit_sha, created_at=row.created_at)
            for row in snapshots
        ],
        pending=pending,
    )


@router.post("/team/snapshots", status_code=202)
async def request_snapshot(
    body: SnapshotIn, session: TeamUser, request: Request, response: Response, db: Db
) -> SnapshotRequest:
    """Snapshot a commit: the default branch's head when none is given."""
    state = request.app.state
    provider: RepoProvider = state.repo_provider
    team, event = await own_team(db, session)
    require_before_deadline(event, state.clock.now())
    if team.registered_at is None or not team.repo_url:
        raise api_error(409, "repo_not_registered", "Register the team's repository first.")

    if body.commit_sha is None:
        try:
            head = await provider.head(team_repo(team))
        except RepoNotFound as exc:
            raise api_error(
                409, "repo_not_found", "The repository can no longer be found."
            ) from exc
        except InfraError as exc:
            raise api_error(
                503, "repo_provider_unavailable", "The repository host is not answering. Try again."
            ) from exc
        if head is None:
            raise api_error(409, "repo_empty", "The repository has no commit yet: push one first.")
        sha = head
    else:
        try:
            sha = check_commit_sha(body.commit_sha)
        except InvalidCommit as exc:
            raise api_error(
                422, "commit_invalid", "Paste the full commit SHA (40 characters)."
            ) from exc

    existing = await _snapshot_id(db, team.id, sha)
    if existing is not None:
        response.status_code = 200
        return SnapshotRequest(commit_sha=sha, status="ready", snapshot_id=existing)

    job_id, _ = await queue.enqueue(
        db,
        "prepare_snapshot",
        queue.prepare_key(team.id, sha),
        payload={"commit_sha": sha},
        team_id=team.id,
    )
    await db.commit()
    job = await db.get(models.Job, job_id)
    assert job is not None
    status = _pending(job)
    if status in ("queued", "running"):
        return SnapshotRequest(
            commit_sha=sha, status=cast(Literal["queued", "running"], status), snapshot_id=None
        )
    if status is None:  # the job finished between the two reads above
        response.status_code = 200
        return SnapshotRequest(
            commit_sha=sha, status="ready", snapshot_id=await _snapshot_id(db, team.id, sha)
        )
    raise api_error(
        409,
        "snapshot_refused",
        f"This commit cannot be snapshotted: {job.error_detail or 'the snapshot failed'}.",
        commit_sha=sha,
    )
