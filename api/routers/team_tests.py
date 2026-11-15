"""A team's drafted tests: read, approved by hash, redrafted after a failure (M7 design, part 3).

Tests belong to the team's claims version (D-047). The team reads the steps and the practice
cases of each; the hidden final cases never leave the server. Approving pins the hash the team
read, until the deadline: the claims lock freezes what a test says, not the approval of it.
"""

import uuid
from typing import cast

from fastapi import APIRouter, Request
from sqlalchemy import select

from api.deps import Db, TeamUser
from api.errors import api_error
from api.team_common import current_claims, own_team, require_before_deadline
from api.team_schemas import (
    ApproveIn,
    ClaimState,
    ClaimTest,
    Drafting,
    TestOut,
    TestsView,
)
from core import queue
from core.db import models

router = APIRouter(prefix="/api", tags=["team"])

# What the newest draft job's status says about drafting; a cancelled job drafted nothing.
_DRAFTING: dict[str, Drafting] = {
    "queued": "queued",
    "running": "running",
    "done": "done",
    "failed": "failed",
    "cancelled": "failed",
}


async def _draft_jobs(db: Db, team_id: uuid.UUID, claims_version: int) -> list[models.Job]:
    """The draft jobs of one claims version, oldest first (the first, then each retry)."""
    result = await db.scalars(
        select(models.Job)
        .where(
            models.Job.team_id == team_id,
            models.Job.kind == "draft_specs",
            models.Job.payload["claims_version"].as_integer() == claims_version,
        )
        .order_by(models.Job.created_at, models.Job.idempotency_key)
    )
    return list(result)


def _test(row: models.TestSpec) -> TestOut:
    return TestOut(
        id=row.id,
        claim_type=row.claim_type,
        version=row.version,
        spec_hash=row.spec_hash,
        steps=row.steps,
        practice_cases=row.practice_cases,
        reason=row.clarification_reason,
        approved_at=row.approved_at,
    )


async def tests_view(db: Db, team_id: uuid.UUID) -> TestsView:
    """Each claim of the team's current claims version, with its newest test and its state."""
    claims = await current_claims(db, team_id)
    if claims is None:
        return TestsView(claims_version=None, drafting="none", claims=[])
    jobs = await _draft_jobs(db, team_id, claims.version)
    drafting: Drafting = _DRAFTING[jobs[-1].status] if jobs else "none"
    rows = await db.scalars(
        select(models.TestSpec)
        .where(
            models.TestSpec.team_id == team_id,
            models.TestSpec.claims_version == claims.version,
            models.TestSpec.claim_id.is_not(None),
        )
        .order_by(models.TestSpec.version)
    )
    newest = {row.claim_id: row for row in rows}  # ordered by version: the last one wins

    entries = []
    for item in claims.items:
        row = newest.get(item["id"])
        state: ClaimState
        if row is not None and row.status in ("draft", "approved", "needs_clarification"):
            state = cast(ClaimState, row.status)
        elif drafting == "done":
            state = "ui_pending"  # the drafter typed it a UI workflow: its test arrives later
        elif drafting == "failed":
            state = "drafting_failed"
        else:
            state = "drafting"
        test = _test(row) if row is not None and state != "ui_pending" else None
        entries.append(ClaimTest(claim_id=item["id"], text=item["text"], state=state, test=test))
    return TestsView(claims_version=claims.version, drafting=drafting, claims=entries)


@router.get("/team/tests")
async def read_tests(session: TeamUser, db: Db) -> TestsView:
    team, _ = await own_team(db, session)
    return await tests_view(db, team.id)


@router.post("/team/tests/{spec_id}/approve")
async def approve_test(
    spec_id: uuid.UUID, body: ApproveIn, session: TeamUser, request: Request, db: Db
) -> TestsView:
    team, event = await own_team(db, session)
    now = request.app.state.clock.now()
    require_before_deadline(event, now)
    row = await db.get(models.TestSpec, spec_id, with_for_update=True)
    if row is None or row.team_id != team.id:
        raise api_error(404, "test_not_found", "No such test.")
    claims = await current_claims(db, team.id)
    current = claims is not None and row.claims_version == claims.version
    if current and row.status == "approved" and row.approved_spec_hash == body.spec_hash:
        return await tests_view(db, team.id)  # approved already, as read
    if not current or row.status not in ("draft", "approved"):
        raise api_error(
            409, "not_approvable", "This test cannot be approved: reload the page to see why."
        )
    if body.spec_hash != row.spec_hash:
        raise api_error(
            409, "hash_mismatch", "The test has changed since it was shown: reload the page."
        )
    row.status = "approved"
    row.approved_at = now
    row.approved_spec_hash = row.spec_hash
    await db.commit()
    return await tests_view(db, team.id)


@router.post("/team/tests/redraft")
async def redraft_tests(session: TeamUser, request: Request, db: Db) -> TestsView:
    """Ask again for the drafts of the current claims version, after the last try failed."""
    state = request.app.state
    cap: int = state.settings.max_redrafts
    team, event = await own_team(db, session)
    require_before_deadline(event, state.clock.now())
    claims = await current_claims(db, team.id)
    jobs = await _draft_jobs(db, team.id, claims.version) if claims is not None else []
    if claims is None or not jobs or jobs[-1].status not in ("failed", "cancelled"):
        raise api_error(409, "nothing_to_redraft", "Only a failed draft can be asked for again.")
    retries = len(jobs) - 1
    if retries >= cap:
        raise api_error(
            409, "too_many_redrafts", f"Drafting was retried {cap} times; ask the organiser."
        )
    await queue.enqueue(
        db,
        "draft_specs",
        f"{queue.draft_key(team.id, claims.version)}:retry{retries + 1}",
        payload={"claims_version": claims.version},
        team_id=team.id,
    )
    await db.commit()
    return await tests_view(db, team.id)
