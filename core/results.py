"""Reading results (section 10): what counts, and the verdicts a submission's claims get.

Only rows whose `attempt_id` is the job's current attempt count, and only once the job is
`done`. Rows from stale attempts stay in the table and are ignored, never deleted. A job that
finally failed has no current rows, so its claims read as `unverified` (section 6.3).
"""

from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import models
from core.verdicts import Verdict, claim_verdicts


async def current_test_results(session: AsyncSession, job_id: UUID) -> list[models.TestResult]:
    rows = await session.scalars(
        select(models.TestResult)
        .join(models.Job, models.Job.id == models.TestResult.job_id)
        .where(
            models.TestResult.job_id == job_id,
            models.Job.status == "done",
            models.TestResult.attempt_id == models.Job.attempt_id,
        )
    )
    return list(rows.all())


async def final_run_job(session: AsyncSession, submission_id: UUID) -> models.Job | None:
    found = await session.scalars(
        select(models.Job)
        .where(models.Job.kind == "final_run", models.Job.submission_id == submission_id)
        .order_by(models.Job.created_at.desc())
        .limit(1)
    )
    return found.first()


async def submission_verdicts(session: AsyncSession, submission_id: UUID) -> dict[str, Verdict]:
    """Each claim of the submission's claims version, with its verdict from the final run."""
    submission = await session.get(models.Submission, submission_id)
    if submission is None:
        return {}
    claims = await session.scalar(
        select(models.Claims).where(
            models.Claims.team_id == submission.team_id,
            models.Claims.version == submission.claims_version,
        )
    )
    claim_ids = [str(item["id"]) for item in (claims.items if claims is not None else [])]

    job = await final_run_job(session, submission_id)
    rows = await current_test_results(session, job.id) if job is not None else []
    spec_ids = {row.spec_id for row in rows}
    claim_of: dict[UUID, str] = {}
    if spec_ids:
        found = await session.execute(
            select(models.TestSpec.id, models.TestSpec.claim_id).where(
                models.TestSpec.id.in_(spec_ids)
            )
        )
        claim_of = {spec_id: claim_id for spec_id, claim_id in found if claim_id}
    pairs = [
        (claim_of[row.spec_id], cast(Verdict, row.verdict))
        for row in rows
        if row.spec_id in claim_of
    ]
    return claim_verdicts(claim_ids, pairs)
