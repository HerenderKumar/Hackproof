"""A team's snapshots and their self-checks (M7 design, parts 4 and 5).

A team picks its default branch's head or pastes a full SHA. The API only asks: the worker's
`prepare_snapshot` job archives the commit, or refuses it with a reason the team can read.

A self-check runs a snapshot against the practice cases of the team's tests. Its routes answer
for `self_check_run` jobs only: a final run's results and logs never reach a team, since the
app may have logged the hidden inputs it was sent.
"""

import asyncio
import uuid
from pathlib import Path
from typing import Any, Literal, cast

from fastapi import APIRouter, Request, Response
from fastapi.responses import PlainTextResponse
from sqlalchemy import select

from api.deps import Db, TeamUser
from api.errors import api_error
from api.team_common import own_team, require_before_deadline
from api.team_schemas import (
    ModelUseOut,
    PendingCommit,
    ResultOut,
    RunOut,
    SelfCheckIn,
    SelfCheckStart,
    SelfCheckView,
    SnapshotIn,
    SnapshotOut,
    SnapshotRequest,
    SnapshotsView,
)
from core import queue
from core.brief_data import ui_page, ui_steps
from core.db import models
from core.errors import InfraError
from core.repo import InvalidCommit, ProviderName, RepoInfo, RepoNotFound, RepoProvider
from core.results import current_test_results
from core.snapshots import check_commit_sha
from core.verdicts import Verdict, claim_verdicts

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


# --- self-checks -----------------------------------------------------------------------------

LOG_LINES = 200
LOG_TAIL_BYTES = 64 * 1024
OUTLINE_LIMIT = 8192  # a team sees the whole outline the runner kept


@router.post("/team/snapshots/{snapshot_id}/self-check", status_code=202)
async def start_self_check(
    snapshot_id: uuid.UUID, body: SelfCheckIn, session: TeamUser, request: Request, db: Db
) -> SelfCheckStart:
    """Run a snapshot against the practice cases. One at a time per team."""
    team, event = await own_team(db, session)
    require_before_deadline(event, request.app.state.clock.now())
    snapshot = await db.get(models.Snapshot, snapshot_id)
    if snapshot is None or snapshot.team_id != team.id:
        raise api_error(404, "snapshot_not_found", "No such snapshot.")

    key = queue.self_check_key(snapshot.id, body.client_request_id)
    # Two clicks at once queue up here, so the second sees the first's job.
    await db.execute(select(models.Team.id).where(models.Team.id == team.id).with_for_update())
    same = await db.scalar(select(models.Job).where(models.Job.idempotency_key == key))
    if same is not None:
        return SelfCheckStart(job_id=same.id, status=same.status)
    busy = await db.scalar(
        select(models.Job.id)
        .where(
            models.Job.team_id == team.id,
            models.Job.kind == "self_check_run",
            models.Job.status.in_(("queued", "running")),
        )
        .limit(1)
    )
    if busy is not None:
        raise api_error(
            409,
            "self_check_in_progress",
            "A self-check is already running: wait for it to finish.",
            job_id=str(busy),
        )
    job_id, _ = await queue.enqueue(
        db, "self_check_run", key, team_id=team.id, snapshot_id=snapshot.id
    )
    await db.commit()
    return SelfCheckStart(job_id=job_id, status="queued")


async def _own_self_check(db: Db, team_id: uuid.UUID, job_id: uuid.UUID) -> models.Job:
    job = await db.get(models.Job, job_id)
    if job is None or job.kind != "self_check_run" or job.team_id != team_id:
        raise api_error(404, "self_check_not_found", "No such self-check.")
    return job


async def _run(db: Db, job: models.Job) -> models.RunResult | None:
    """The run of the job's current attempt, once the job is done."""
    if job.status != "done" or job.attempt_id is None:
        return None
    found: models.RunResult | None = await db.scalar(
        select(models.RunResult).where(
            models.RunResult.job_id == job.id, models.RunResult.attempt_id == job.attempt_id
        )
    )
    return found


@router.get("/team/self-checks/{job_id}")
async def read_self_check(job_id: uuid.UUID, session: TeamUser, db: Db) -> SelfCheckView:
    team, _ = await own_team(db, session)
    job = await _own_self_check(db, team.id, job_id)
    snapshot = await db.get(models.Snapshot, job.snapshot_id) if job.snapshot_id else None
    run = await _run(db, job)
    rows = await current_test_results(db, job.id)
    claim_of: dict[uuid.UUID, str] = {}
    kind_of: dict[uuid.UUID, str] = {}
    if rows:
        found = await db.execute(
            select(models.TestSpec.id, models.TestSpec.claim_id, models.TestSpec.kind).where(
                models.TestSpec.id.in_({row.spec_id for row in rows})
            )
        )
        for spec_id, claim_id, kind in found:
            if claim_id:
                claim_of[spec_id], kind_of[spec_id] = claim_id, kind
    results = sorted(
        (
            ResultOut(
                result_id=row.id,
                claim_id=claim_of[row.spec_id],
                case_id=row.case_id,
                verdict=row.verdict,
                assertion_results=row.assertion_results,
                error=row.error,
                duration_ms=row.duration_ms,
                **_ui_fields(row, kind_of[row.spec_id]),
            )
            for row in rows
            if row.spec_id in claim_of
        ),
        key=lambda result: (result.claim_id, result.case_id or ""),
    )
    claim_ids = list(dict.fromkeys(result.claim_id for result in results))
    pairs = [(result.claim_id, cast(Verdict, result.verdict)) for result in results]
    failed = job.status in ("failed", "cancelled") or job.error_class is not None
    return SelfCheckView(
        job_id=job.id,
        snapshot_id=job.snapshot_id,
        commit_sha=snapshot.commit_sha if snapshot else None,
        status=job.status,
        error=job.error_detail if failed else None,
        run=None
        if run is None
        else RunOut(
            build_status=run.build_status,
            startup_status=run.startup_status,
            startup_detail=run.startup_detail,
            model_use=ModelUseOut.of(run.model_use),
        ),
        results=results,
        claim_verdicts=dict(claim_verdicts(claim_ids, pairs)),
    )


def _ui_fields(row: models.TestResult, kind: str) -> dict[str, Any]:
    """A practice UI case's evidence for its own team (M11 design, part 8)."""
    if kind != "ui":
        return {}
    return {
        "kind": "ui",
        "steps": ui_steps(row.actual),
        "page": ui_page(row.actual, outline_chars=OUTLINE_LIMIT),
        "has_video": bool(row.video_path),
        "has_trace": bool(row.trace_path),
    }


def _tail(root: Path, stored: str) -> str | None:
    """The last LOG_LINES lines within the last LOG_TAIL_BYTES of a log, or None when the log
    is missing or lies outside the artifact directory."""
    path = Path(stored).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        return None
    with path.open("rb") as log:
        size = log.seek(0, 2)
        log.seek(max(size - LOG_TAIL_BYTES, 0))
        data = log.read(LOG_TAIL_BYTES)
    lines = data.decode(errors="replace").splitlines()
    if size > LOG_TAIL_BYTES:
        lines = lines[1:]  # cut mid-line
    return "".join(f"{line}\n" for line in lines[-LOG_LINES:])


@router.get("/team/self-checks/{job_id}/logs/{kind}", response_class=PlainTextResponse)
async def read_self_check_log(
    job_id: uuid.UUID, kind: str, session: TeamUser, request: Request, db: Db
) -> PlainTextResponse:
    """The end of a self-check's build or app log, as text."""
    team, _ = await own_team(db, session)
    job = await _own_self_check(db, team.id, job_id)
    run = await _run(db, job)
    stored = {"build": run.build_log_path, "app": run.app_log_path}.get(kind) if run else None
    root: Path = request.app.state.settings.artifact_dir
    text = await asyncio.to_thread(_tail, root, stored) if stored else None
    if text is None:
        raise api_error(404, "log_not_found", "That log is not available.")
    return PlainTextResponse(text)
