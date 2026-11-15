"""What every team route shares: the signed-in team, its current claims, and the event's gates.

Every query starts from the session's team ID (section 12.4); no route takes a team ID from the
request. The gates use the API's clock; only the final submission's deadline uses the
database's (see `api.routers.team_submissions`).
"""

import uuid
from datetime import datetime

from sqlalchemy import select

from api.auth import Session
from api.deps import Db
from api.errors import api_error
from core.db import models


async def own_team(db: Db, session: Session) -> tuple[models.Team, models.Event]:
    """The signed-in team and its event."""
    team = await db.get(models.Team, session.subject_id)
    event = await db.get(models.Event, team.event_id) if team is not None else None
    if team is None or event is None:
        raise api_error(401, "not_signed_in", "Sign in to continue.")
    return team, event


async def current_claims(db: Db, team_id: uuid.UUID) -> models.Claims | None:
    """The team's latest claims version, or None before its first."""
    result = await db.scalars(
        select(models.Claims)
        .where(models.Claims.team_id == team_id)
        .order_by(models.Claims.version.desc())
        .limit(1)
    )
    return result.first()


def require_started(team: models.Team, event: models.Event, now: datetime) -> None:
    if team.rules_acknowledged_at is None:
        raise api_error(409, "rules_not_acknowledged", "Acknowledge the event rules first.")
    if now < event.kickoff_at:
        raise api_error(409, "event_not_started", "The event has not started yet.")


def require_before_deadline(event: models.Event, now: datetime) -> None:
    if now > event.deadline_at:
        raise api_error(409, "deadline_passed", "The event's deadline has passed.")
