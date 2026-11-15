"""The demo seed's own parts (M12): its clock, its job loop and its model transport."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core import queue
from core.llm import ReplayMissing
from scripts.demo_event import KICKOFF
from scripts.seed_demo import SeedClock, SeedError, drain, filling_transport
from tests.factories import make_event, make_team
from tests.helpers import enqueue, job_row
from worker.lanes import Handler

Sessions = async_sessionmaker[AsyncSession]


async def _two_teams(sessions: Sessions) -> tuple[uuid.UUID, uuid.UUID]:
    async with sessions.begin() as s:
        event = make_event()
        s.add(event)
        await s.flush()
        ours, theirs = make_team(event.id), make_team(event.id)
        s.add_all([ours, theirs])
        await s.flush()
        return ours.id, theirs.id


def test_the_clock_stands_still_when_frozen_and_runs_when_live() -> None:
    clock = SeedClock()
    clock.freeze(KICKOFF)
    assert clock.now() == KICKOFF
    assert clock.now() == KICKOFF
    clock.live()
    assert abs(clock.now() - datetime.now(UTC)) < timedelta(seconds=2)


async def test_drain_runs_only_the_named_teams_jobs(sessions: Sessions) -> None:
    ours, theirs = await _two_teams(sessions)
    mine = await enqueue(sessions, "poll_repo", team_id=ours)
    other = await enqueue(sessions, "poll_repo", team_id=theirs)
    seen: list[uuid.UUID | None] = []

    async def poll(job: queue.Job) -> None:
        seen.append(job.team_id)

    await drain(sessions, {"poll_repo": poll}, [ours], idle_s=0.05)
    assert seen == [ours]
    assert (await job_row(sessions, mine)).status == "done"
    assert (await job_row(sessions, other)).status == "queued"


async def test_drain_runs_what_a_job_enqueues_and_what_waits_on_it(sessions: Sessions) -> None:
    ours, _ = await _two_teams(sessions)
    first = await enqueue(sessions, "poll_repo", team_id=ours)
    await enqueue(sessions, "draft_specs", team_id=ours, depends_on=first)
    ran: list[str] = []

    def recorder(kind: str) -> Handler:
        async def handle(job: queue.Job) -> None:
            ran.append(kind)
            if kind == "poll_repo":
                await enqueue(sessions, "kickoff_snapshot", team_id=ours)

        return handle

    kinds = ("poll_repo", "kickoff_snapshot", "draft_specs")
    await drain(sessions, {k: recorder(k) for k in kinds}, [ours], idle_s=0.05)
    assert ran[0] == "poll_repo"
    assert sorted(ran) == sorted(kinds)


async def test_drain_gives_up_and_names_what_is_waiting(sessions: Sessions) -> None:
    ours, _ = await _two_teams(sessions)
    later = datetime.now(UTC) + timedelta(hours=1)
    await enqueue(sessions, "draft_specs", team_id=ours, run_after=later)

    async def never(job: queue.Job) -> None:
        raise AssertionError("a job not yet due was run")

    with pytest.raises(SeedError, match="draft_specs"):
        await drain(sessions, {"draft_specs": never}, [ours], timeout_s=0.3, idle_s=0.05)


class _Side:
    def __init__(self, answer: dict[str, Any] | Exception) -> None:
        self.answer = answer
        self.calls = 0

    async def __call__(self, body: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


async def test_a_miss_is_filled_from_the_live_side_and_a_hit_is_not() -> None:
    body = {"model": "m", "messages": []}
    fresh = _Side({"from": "fresh"})
    filled = await filling_transport(_Side(ReplayMissing("no")), fresh)(body)
    assert filled == {"from": "fresh"} and fresh.calls == 1

    fresh = _Side({"from": "fresh"})
    hit = await filling_transport(_Side({"from": "recorded"}), fresh)(body)
    assert hit == {"from": "recorded"} and fresh.calls == 0
