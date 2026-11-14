"""Scheduler rules (section 11.5): one kickoff snapshot and bucketed polls per registered team."""

from datetime import datetime, timedelta
from typing import Any

from core import queue
from core.db import models
from tests.factories import T0, make_event, make_team
from tests.helpers import Sessions, all_jobs
from worker import scheduler

DEADLINE = T0 + timedelta(hours=24)


async def registered_team(
    sessions: Sessions, *, registered: bool = True, **event: Any
) -> models.Team:
    async with sessions.begin() as s:
        row = make_event(kickoff_at=T0, deadline_at=DEADLINE, **event)
        s.add(row)
        await s.flush()
        team = make_team(
            row.id,
            repo_url="https://github.com/acme/app" if registered else None,
            registered_at=T0 - timedelta(days=1) if registered else None,
        )
        s.add(team)
    return team


async def tick(sessions: Sessions, now: datetime) -> int:
    async with sessions.begin() as s:
        return await scheduler.tick(s, now)


async def jobs_of_kind(sessions: Sessions, kind: str) -> list[models.Job]:
    return [job for job in await all_jobs(sessions) if job.kind == kind]


async def test_a_registered_team_gets_one_kickoff_snapshot_once_the_event_starts(
    sessions: Sessions,
) -> None:
    team = await registered_team(sessions)

    await tick(sessions, T0)
    await tick(sessions, T0 + timedelta(seconds=30))

    [job] = await jobs_of_kind(sessions, "kickoff_snapshot")
    assert (job.team_id, job.idempotency_key) == (team.id, f"kickoff:{team.id}")


async def test_nothing_is_scheduled_before_kickoff(sessions: Sessions) -> None:
    await registered_team(sessions)

    created = await tick(sessions, T0 - timedelta(seconds=1))

    assert created == 0
    assert await all_jobs(sessions) == []


async def test_a_team_without_a_registered_repo_is_skipped(sessions: Sessions) -> None:
    await registered_team(sessions, registered=False)

    await tick(sessions, T0 + timedelta(minutes=1))

    assert await all_jobs(sessions) == []


async def test_no_kickoff_snapshot_is_scheduled_once_a_baseline_observation_exists(
    sessions: Sessions,
) -> None:
    for kind in ("kickoff", "late_registration"):
        team = await registered_team(sessions)
        async with sessions.begin() as s:
            s.add(models.RepoObservation(team_id=team.id, kind=kind, observed_at=T0))

    await tick(sessions, T0 + timedelta(minutes=1))

    assert await jobs_of_kind(sessions, "kickoff_snapshot") == []


async def test_a_poll_is_scheduled_once_per_poll_interval(sessions: Sessions) -> None:
    team = await registered_team(sessions, poll_interval_seconds=300)

    await tick(sessions, T0 + timedelta(seconds=10))
    await tick(sessions, T0 + timedelta(seconds=40))
    assert len(await jobs_of_kind(sessions, "poll_repo")) == 1

    later = T0 + timedelta(seconds=310)
    await tick(sessions, later)

    polls = await jobs_of_kind(sessions, "poll_repo")
    assert [p.team_id for p in polls] == [team.id, team.id]
    assert queue.poll_key(team.id, later, 300) in {p.idempotency_key for p in polls}


async def test_polls_follow_the_events_own_interval(sessions: Sessions) -> None:
    await registered_team(sessions, poll_interval_seconds=60)

    await tick(sessions, T0 + timedelta(seconds=10))
    await tick(sessions, T0 + timedelta(seconds=70))

    assert len(await jobs_of_kind(sessions, "poll_repo")) == 2


async def test_polling_continues_for_thirty_minutes_after_the_deadline(
    sessions: Sessions,
) -> None:
    await registered_team(sessions)

    await tick(sessions, DEADLINE + timedelta(minutes=29))
    assert len(await jobs_of_kind(sessions, "poll_repo")) == 1

    await tick(sessions, DEADLINE + timedelta(minutes=31))
    assert len(await jobs_of_kind(sessions, "poll_repo")) == 1


async def test_tick_reports_how_many_jobs_it_created(sessions: Sessions) -> None:
    await registered_team(sessions)
    await registered_team(sessions)

    assert await tick(sessions, T0 + timedelta(seconds=5)) == 4  # a snapshot and a poll each
    assert await tick(sessions, T0 + timedelta(seconds=35)) == 0
