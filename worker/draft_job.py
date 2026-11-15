"""The `draft_specs` job: draft a team's tests from its claims version (D-047).

The job belongs to one team and one claims version. It reads that version's start text (the
problem statement, the explanation and the claims), asks the model for one test per claim, and
stores the answers as `test_specs` rows owned by the team's claims version: no snapshot is
loaded, because no code is read. Rows from the team's earlier claims versions are marked
`superseded`.

Two things make the job write nothing: rows already stored for this claims version (a repeated
attempt), and a newer claims version (which has its own job). Both are checked before the model
is called, to spend nothing, and again inside the writing transaction, which holds the claims
row's lock, so two attempts cannot both write. Gateway failures are left to the job loop.
"""

import uuid
from collections import Counter
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core import queue
from core.claims import claim_fields
from core.db import models
from core.db.engine import Sessions
from core.drafting import Drafted, StartText, draft_tests
from core.llm import LLMClient
from core.logging import get_logger
from core.settings import Settings
from core.specs import spec_hash
from worker.lanes import Handler, Outcome

log = get_logger()


def make_draft_handler(sessions: Sessions, client: LLMClient, settings: Settings) -> Handler:
    async def draft_specs(job: queue.Job) -> Outcome | None:
        team_id, version = job.team_id, job.payload.get("claims_version")
        if team_id is None or not isinstance(version, int) or isinstance(version, bool):
            return Outcome("validation", "draft_specs needs a team and a claims version")

        async with sessions() as session:
            claims = await session.scalar(
                select(models.Claims).where(
                    models.Claims.team_id == team_id, models.Claims.version == version
                )
            )
            if claims is None:
                return Outcome("validation", f"the team has no claims version {version}")
            reason = await _nothing_to_do(session, team_id, version)
        if reason is not None:
            log.info("draft_skipped", claims_version=version, reason=reason)
            return None

        start = StartText(
            problem_statement=claims.problem_statement,
            explanation=claims.explanation,
            claims=[claim_fields(item) for item in claims.items],
        )
        drafts = await draft_tests(
            client,
            start,
            model=settings.draft_model,
            max_tokens=settings.draft_max_tokens,
            job_id=job.id,
        )

        async with sessions.begin() as session:
            await session.execute(
                select(models.Claims.id).where(models.Claims.id == claims.id).with_for_update()
            )
            reason = await _nothing_to_do(session, team_id, version)
            if reason is not None:
                log.info("draft_skipped", claims_version=version, reason=reason)
                return None
            highest = await _highest_versions(session, team_id)
            for drafted in drafts:
                if drafted.status == "ui_pending":  # typed now; its test arrives with M11
                    continue
                session.add(_row(team_id, version, drafted, highest.get(drafted.claim_id, 0) + 1))
            await session.execute(
                update(models.TestSpec)
                .where(
                    models.TestSpec.team_id == team_id,
                    models.TestSpec.claims_version < version,
                    models.TestSpec.status != "superseded",
                )
                .values(status="superseded")
            )
        log.info(
            "specs_drafted",
            claims_version=version,
            **{status: count for status, count in Counter(d.status for d in drafts).items()},
        )
        return None

    return draft_specs


async def _nothing_to_do(session: AsyncSession, team_id: uuid.UUID, version: int) -> str | None:
    """Why this claims version needs no drafting now, or None when it does."""
    newest = await session.scalar(
        select(func.max(models.Claims.version)).where(models.Claims.team_id == team_id)
    )
    if newest is not None and newest > version:
        return "a newer claims version exists"
    stored = await session.scalar(
        select(func.count())
        .select_from(models.TestSpec)
        .where(models.TestSpec.team_id == team_id, models.TestSpec.claims_version == version)
    )
    return "tests are already stored for this claims version" if stored else None


async def _highest_versions(session: AsyncSession, team_id: uuid.UUID) -> dict[str | None, int]:
    """Each claim's highest test version so far, over all the team's claims versions."""
    rows = await session.execute(
        select(models.TestSpec.claim_id, func.max(models.TestSpec.version))
        .where(models.TestSpec.team_id == team_id)
        .group_by(models.TestSpec.claim_id)
    )
    return {claim_id: highest for claim_id, highest in rows.all()}


def _row(
    team_id: uuid.UUID, claims_version: int, drafted: Drafted, version: int
) -> models.TestSpec:
    # A claim needing clarification has no test: empty steps and cases, hashed as they are.
    document: dict[str, Any] = drafted.spec or {
        "kind": "api",
        "claim_type": drafted.claim_type,
        "steps": [],
        "practice_cases": [],
        "final_cases": [],
    }
    return models.TestSpec(
        team_id=team_id,
        snapshot_id=None,
        claims_version=claims_version,
        version=version,
        kind="api",
        claim_id=drafted.claim_id,
        claim_type=drafted.claim_type,
        steps=document["steps"],
        practice_cases=document["practice_cases"],
        final_cases=document["final_cases"],
        spec_hash=spec_hash(document),
        status=drafted.status,
        clarification_reason=drafted.reason or None,
    )
