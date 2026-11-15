"""The scheduler (section 11.5): keeps kickoff snapshots and repo polls flowing, and deletes
device code once an event's retention has passed (M9).

Idempotency keys make every tick safe to repeat: one kickoff snapshot per team, one poll per
team per poll interval, and one purge per event per day.
"""

from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core import queue
from core.clock import Clock
from core.db.engine import Sessions
from core.logging import get_logger

INTERVAL_S = 30.0
# Polling runs past the deadline so late pushes are seen and recorded.
POLL_GRACE = timedelta(minutes=30)

log = get_logger()

_STARTED_TEAMS = text(
    """
    SELECT t.id AS team_id, e.deadline_at, e.poll_interval_seconds,
           EXISTS (SELECT 1 FROM repo_observations o
                   WHERE o.team_id = t.id
                     AND o.kind IN ('kickoff', 'late_registration')) AS has_baseline
    FROM teams t JOIN events e ON e.id = t.event_id
    WHERE t.registered_at IS NOT NULL AND e.kickoff_at <= :now
    """
)


# Events whose retention has passed and that still hold a baseline not yet purged.
_PURGEABLE = text(
    """
    SELECT e.id AS event_id FROM events e
    WHERE e.deadline_at + make_interval(days => e.baseline_retention_days) <= :now
      AND EXISTS (SELECT 1 FROM baselines b JOIN teams t ON t.id = b.team_id
                  WHERE t.event_id = e.id AND b.status <> 'purged')
    """
)


async def tick(session: AsyncSession, now: datetime) -> int:
    """Enqueue what is due at `now`. Returns the number of jobs that are new."""
    created = 0
    for team in (await session.execute(_STARTED_TEAMS, {"now": now})).all():
        if not team.has_baseline:
            _, new = await queue.enqueue(
                session, "kickoff_snapshot", queue.kickoff_key(team.team_id), team_id=team.team_id
            )
            created += new
        if now <= team.deadline_at + POLL_GRACE:
            key = queue.poll_key(team.team_id, now, team.poll_interval_seconds)
            _, new = await queue.enqueue(session, "poll_repo", key, team_id=team.team_id)
            created += new
    for event in (await session.execute(_PURGEABLE, {"now": now})).all():
        _, new = await queue.enqueue(
            session,
            "purge_baselines",
            queue.purge_key(event.event_id, now.date()),
            payload={"event_id": str(event.event_id)},
        )
        created += new
    return created


async def run_once(sessions: Sessions, clock: Clock) -> None:
    async with sessions.begin() as session:
        created = await tick(session, clock.now())
    if created:
        log.info("scheduler_enqueued", jobs=created)
