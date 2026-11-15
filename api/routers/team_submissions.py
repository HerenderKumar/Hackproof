"""The final submission and its receipt (M7 design, part 6; invariant 3).

A submission is one INSERT, never an UPDATE, made in one transaction with its `final_run`
job and its `audit` job, which waits for the run (section 11.6). Its deadline is judged by the
database's clock. A repeated idempotency key returns the stored receipt whatever the time, so
a team that lost the answer can always get it back. A new submission before the deadline is a
new row; the latest one by the deadline is judged.
"""

import uuid

from fastapi import APIRouter, Request, Response
from sqlalchemy import func, select

from api.deps import Db, TeamUser
from api.errors import api_error
from api.routers.team_tests import tests_view
from api.team_common import current_claims, own_team
from api.team_schemas import CurrentSubmission, Receipt, SubmissionIn
from core import queue
from core.db import models
from core.receipts import receipt_fields, receipt_hash
from core.results import final_run_job
from core.specs import test_spec_hash

router = APIRouter(prefix="/api", tags=["team"])


def _receipt(
    submission: models.Submission, snapshot: models.Snapshot, team: models.Team
) -> Receipt:
    return Receipt(
        **receipt_fields(submission, snapshot),
        team_name=team.name,
        receipt_hash=submission.receipt_hash,
    )


async def _approved_hashes(db: Db, team_id: uuid.UUID, claims_version: int) -> list[str]:
    """Per claim, the newest test approved as it reads now: what the final run will carry
    (`worker.run_specs.final_run_plan` selects the same rows and checks this hash)."""
    rows = await db.scalars(
        select(models.TestSpec)
        .where(
            models.TestSpec.team_id == team_id,
            models.TestSpec.claims_version == claims_version,
            models.TestSpec.kind != "startup",
            models.TestSpec.approved_at.is_not(None),
            models.TestSpec.approved_spec_hash == models.TestSpec.spec_hash,
        )
        .order_by(models.TestSpec.claim_id, models.TestSpec.version)
    )
    newest = {row.claim_id: row for row in rows}
    return [row.spec_hash for row in newest.values()]


async def _count(db: Db, team_id: uuid.UUID) -> int:
    made: int = await db.scalar(
        select(func.count())
        .select_from(models.Submission)
        .where(models.Submission.team_id == team_id)
    )
    return made


@router.post("/team/submissions", status_code=201)
async def submit(
    body: SubmissionIn, session: TeamUser, request: Request, response: Response, db: Db
) -> Receipt:
    settings = request.app.state.settings
    team, event = await own_team(db, session)
    # One submission at a time per team: the key lookup, the cap and the insert of one request
    # see everything the one before it wrote, so two clicks with one key store one row.
    await db.execute(select(models.Team.id).where(models.Team.id == team.id).with_for_update())
    key = f"{team.id}:{body.idempotency_key}"
    stored = await db.scalar(
        select(models.Submission).where(models.Submission.idempotency_key == key)
    )
    if stored is not None:
        snapshot = await db.get(models.Snapshot, stored.snapshot_id)
        assert snapshot is not None
        response.status_code = 200
        return _receipt(stored, snapshot, team)

    now = await db.scalar(select(func.now()))  # the database's clock, not the API server's
    if now > event.deadline_at:
        raise api_error(409, "deadline_passed", "The deadline has passed: submissions are closed.")
    snapshot = await db.get(models.Snapshot, body.snapshot_id)
    if snapshot is None or snapshot.team_id != team.id:
        raise api_error(404, "snapshot_not_found", "No such snapshot.")
    claims = await current_claims(db, team.id)
    if claims is None:
        raise api_error(409, "no_claims", "Write the team's claims before submitting.")
    view = await tests_view(db, team.id)
    if view.drafting != "done":
        raise api_error(
            409, "tests_not_ready", "The tests are not drafted yet: wait for them, or try again."
        )
    drafts = [claim.claim_id for claim in view.claims if claim.state == "draft"]
    if drafts:
        raise api_error(
            409,
            "tests_not_approved",
            "Read and approve every drafted test first.",
            claims=drafts,
        )
    cap: int = settings.max_submissions
    if await _count(db, team.id) >= cap:
        raise api_error(409, "too_many_submissions", f"A team can submit at most {cap} times.")

    submission = models.Submission(
        id=uuid.uuid4(),
        team_id=team.id,
        snapshot_id=snapshot.id,
        mode=body.mode,
        deploy_url=None,
        claims_version=claims.version,
        claims_hash=claims.claims_hash,
        test_spec_hash=test_spec_hash(await _approved_hashes(db, team.id, claims.version)),
        rubric_version=event.rubric_version,
        runner_version=settings.runner_version,
        railpack_version=settings.railpack_version,
        reuse_declaration=body.reuse_declaration.model_dump(),
        submitted_at=now,
        idempotency_key=key,
        receipt_hash="",
    )
    submission.receipt_hash = receipt_hash(receipt_fields(submission, snapshot))
    db.add(submission)
    await db.flush()  # the row exists before the job that names it
    final_run_id, _ = await queue.enqueue(
        db,
        "final_run",
        queue.final_run_key(submission.id),
        submission_id=submission.id,
        team_id=team.id,
    )
    audit_id, _ = await queue.enqueue(
        db,
        "audit",
        queue.audit_key(submission.id),
        submission_id=submission.id,
        team_id=team.id,
        depends_on=final_run_id,
    )
    # The brief's written part waits for the audit, done or failed: it reads what there is.
    await queue.enqueue(
        db,
        "brief",
        queue.brief_key(submission.id),
        submission_id=submission.id,
        team_id=team.id,
        depends_on=audit_id,
    )
    # What of the final code was already on the team's devices before kickoff (M9).
    await queue.enqueue(
        db,
        "prior_work",
        queue.prior_work_key(submission.id),
        submission_id=submission.id,
        team_id=team.id,
    )
    await db.commit()
    return _receipt(submission, snapshot, team)


@router.get("/team/submissions/current")
async def current_submission(session: TeamUser, db: Db) -> CurrentSubmission:
    """The submission that will be judged: the latest made by the deadline."""
    team, event = await own_team(db, session)
    latest = await db.scalar(
        select(models.Submission)
        .where(
            models.Submission.team_id == team.id,
            models.Submission.submitted_at <= event.deadline_at,
        )
        .order_by(models.Submission.submitted_at.desc(), models.Submission.id.desc())
        .limit(1)
    )
    if latest is None:
        raise api_error(404, "no_submission", "The team has not submitted yet.")
    snapshot = await db.get(models.Snapshot, latest.snapshot_id)
    assert snapshot is not None
    job = await final_run_job(db, latest.id)
    return CurrentSubmission(
        receipt=_receipt(latest, snapshot, team),
        submissions_made=await _count(db, team.id),
        final_run=job.status if job is not None else "none",
    )
