"""A team's claims (M7 design, part 2; D-047).

Each change is a new claims version and asks the worker to draft its tests (`draft_specs`),
from kickoff until the event's claims lock, and at most `max_claims_versions` times: each
version is a model call paid for by the organiser.
"""

from datetime import datetime

from fastapi import APIRouter, Request
from sqlalchemy import select

from api.deps import Db, TeamUser
from api.errors import api_error
from api.team_common import current_claims, own_team, require_started
from api.team_schemas import ClaimsIn, ClaimsOut, ClaimsView
from core import queue
from core.claims import claims_hash
from core.db import models
from core.events import claims_lock_at

router = APIRouter(prefix="/api", tags=["team"])


def _view(claims: models.Claims | None, event: models.Event, now: datetime, cap: int) -> ClaimsView:
    lock_at = claims_lock_at(event)
    return ClaimsView(
        current=None if claims is None else ClaimsOut.model_validate(claims, from_attributes=True),
        lock_at=lock_at,
        locked=now >= lock_at,
        versions_left=max(cap - (claims.version if claims else 0), 0),
    )


@router.get("/team/claims")
async def read_claims(session: TeamUser, request: Request, db: Db) -> ClaimsView:
    state = request.app.state
    team, event = await own_team(db, session)
    claims = await current_claims(db, team.id)
    return _view(claims, event, state.clock.now(), state.settings.max_claims_versions)


@router.put("/team/claims")
async def save_claims(body: ClaimsIn, session: TeamUser, request: Request, db: Db) -> ClaimsView:
    state = request.app.state
    cap: int = state.settings.max_claims_versions
    team, event = await own_team(db, session)
    now = state.clock.now()
    require_started(team, event, now)
    if now >= claims_lock_at(event):
        raise api_error(409, "claims_locked", "Claims are locked: they can no longer change.")

    # Two saves at once queue up here, so each reads the version the other wrote.
    await db.execute(select(models.Team.id).where(models.Team.id == team.id).with_for_update())
    current = await current_claims(db, team.id)
    items = [{"id": f"c{n}", **item.model_dump()} for n, item in enumerate(body.items, start=1)]
    digest = claims_hash(body.problem_statement, body.explanation, items)
    if current is not None and current.claims_hash == digest:
        return _view(current, event, now, cap)

    version = (current.version if current else 0) + 1
    if version > cap:
        raise api_error(409, "too_many_claim_edits", f"Claims can be changed at most {cap} times.")
    claims = models.Claims(
        team_id=team.id,
        version=version,
        items=items,
        claims_hash=digest,
        problem_statement=body.problem_statement,
        explanation=body.explanation,
    )
    db.add(claims)
    await queue.enqueue(
        db,
        "draft_specs",
        queue.draft_key(team.id, version),
        payload={"claims_version": version},
        team_id=team.id,
    )
    await db.commit()
    await db.refresh(claims)
    return _view(claims, event, now, cap)
