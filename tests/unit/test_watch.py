"""The repo watch's two jobs, `kickoff_snapshot` and `poll_repo` (sections 6.6 and 13)."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core import queue
from core.db import models
from core.errors import InfraError
from core.repo import Comparison, RepoNotFound
from tests.factories import T0, make_event, make_team
from tests.fakes import FakeClock, FakeRepoProvider
from worker.lanes import Outcome
from worker.watch import make_kickoff_handler, make_poll_handler

Sessions = async_sessionmaker[AsyncSession]
URL = "https://github.com/acme/rocket"
A, B, C = "a" * 40, "b" * 40, "c" * 40


def commit(sha: str, author_date: str = "2026-11-14T09:10:00+00:00") -> dict[str, str]:
    return {"sha": sha, "author_date": author_date, "committer_date": author_date, "message": "m"}


def ahead(lines: int, *shas: str) -> Comparison:
    return Comparison(
        status="ahead", lines_added=lines, files_changed=1, commits=[commit(s) for s in shas]
    )


class Watch:
    def __init__(self, sessions: Sessions) -> None:
        self.sessions = sessions
        self.provider = FakeRepoProvider()
        self.provider.add(URL)
        self.clock = FakeClock(T0)
        self.kickoff = make_kickoff_handler(sessions, self.provider, self.clock)
        self.poll = make_poll_handler(sessions, self.provider, self.clock)
        self.team_id = uuid.uuid4()

    async def register(self, *, registered_at: object = T0 - timedelta(days=1)) -> None:
        async with self.sessions.begin() as session:
            event = make_event()
            session.add(event)
            await session.flush()
            team = make_team(
                event.id,
                id=self.team_id,
                repo_url=URL,
                repo_provider="github",
                repository_id="1000",
                default_branch="main",
                registered_at=registered_at,
            )
            session.add(team)

    def job(self, kind: str) -> queue.Job:
        return queue.Job.model_construct(kind=kind, team_id=self.team_id, payload={})

    async def run_kickoff(self) -> Outcome | None:
        return await self.kickoff(self.job("kickoff_snapshot"))

    async def run_poll(self, minutes: int) -> Outcome | None:
        self.clock.at = T0 + timedelta(minutes=minutes)
        return await self.poll(self.job("poll_repo"))

    def head(self, sha: str | None) -> None:
        self.provider.heads[URL] = sha

    async def rows(self) -> list[models.RepoObservation]:
        async with self.sessions() as session:
            found = await session.scalars(
                select(models.RepoObservation).order_by(models.RepoObservation.id)
            )
            return list(found)


@pytest.fixture
async def watch(sessions: Sessions) -> Watch:
    w = Watch(sessions)
    await w.register()
    return w


async def test_kickoff_records_the_head_its_lines_and_its_commits(watch: Watch) -> None:
    watch.head(A)
    watch.provider.comparisons[(None, A)] = ahead(120, A)

    assert await watch.run_kickoff() is None

    [row] = await watch.rows()
    assert (row.kind, row.head_sha, row.lines_total, row.observed_at) == ("kickoff", A, 120, T0)
    assert [c["sha"] for c in row.commits] == [A]
    assert row.descends_from_previous is None


async def test_kickoff_of_an_empty_repository_records_no_head(watch: Watch) -> None:
    watch.head(None)

    await watch.run_kickoff()

    [row] = await watch.rows()
    assert (row.kind, row.head_sha, row.lines_total, row.commits) == ("kickoff", None, 0, [])


async def test_a_repository_registered_after_kickoff_is_a_late_registration(
    sessions: Sessions,
) -> None:
    watch = Watch(sessions)
    await watch.register(registered_at=T0 + timedelta(hours=2))
    watch.head(None)

    await watch.run_kickoff()

    assert [r.kind for r in await watch.rows()] == ["late_registration"]


async def test_kickoff_runs_once(watch: Watch) -> None:
    watch.head(None)
    await watch.run_kickoff()
    watch.head(A)  # a second attempt would see another head; it must not write
    watch.provider.comparisons[(None, A)] = ahead(5, A)
    await watch.run_kickoff()

    assert [r.head_sha for r in await watch.rows()] == [None]


async def test_a_poll_before_the_kickoff_row_writes_nothing(watch: Watch) -> None:
    watch.head(A)

    assert await watch.run_poll(5) is None
    assert await watch.rows() == []


async def test_an_unchanged_head_writes_nothing(watch: Watch) -> None:
    watch.head(A)
    watch.provider.comparisons[(None, A)] = ahead(10, A)
    await watch.run_kickoff()

    await watch.run_poll(5)

    assert len(await watch.rows()) == 1


async def test_a_new_head_that_descends_is_recorded_as_such(watch: Watch) -> None:
    watch.head(A)
    watch.provider.comparisons[(None, A)] = ahead(10, A)
    await watch.run_kickoff()
    watch.head(B)
    watch.provider.comparisons[(A, B)] = ahead(30, B)

    await watch.run_poll(5)

    row = (await watch.rows())[-1]
    assert (row.kind, row.head_sha, row.previous_head_sha) == ("poll", B, A)
    assert (row.descends_from_previous, row.lines_added, row.files_changed) == (True, 30, 1)
    assert row.observed_at == T0 + timedelta(minutes=5)
    assert [c["sha"] for c in row.commits] == [B]


async def test_a_rewritten_history_is_recorded_and_earlier_heads_stay(watch: Watch) -> None:
    watch.head(None)
    await watch.run_kickoff()
    watch.head(A)
    watch.provider.comparisons[(None, A)] = ahead(10, A)
    await watch.run_poll(5)
    watch.head(B)
    watch.provider.comparisons[(A, B)] = ahead(20, B)
    await watch.run_poll(10)
    before = [(r.id, r.head_sha, r.descends_from_previous) for r in await watch.rows()]
    watch.head(C)
    watch.provider.comparisons[(B, C)] = Comparison(
        status="diverged", lines_added=4, files_changed=2, commits=[commit(C)]
    )

    await watch.run_poll(15)

    rows = await watch.rows()
    assert [(r.id, r.head_sha, r.descends_from_previous) for r in rows[:-1]] == before
    last = rows[-1]
    assert (last.head_sha, last.previous_head_sha, last.descends_from_previous) == (C, B, False)


async def test_a_first_push_into_an_empty_repository_descends(watch: Watch) -> None:
    watch.head(None)
    await watch.run_kickoff()
    watch.head(A)
    watch.provider.comparisons[(None, A)] = ahead(2500, A)

    await watch.run_poll(5)

    row = (await watch.rows())[-1]
    assert (row.previous_head_sha, row.descends_from_previous, row.lines_added) == (
        None,
        True,
        2500,
    )


async def test_a_head_that_is_gone_is_recorded(watch: Watch) -> None:
    watch.head(A)
    watch.provider.comparisons[(None, A)] = ahead(10, A)
    await watch.run_kickoff()
    watch.head(None)  # the branch was deleted

    await watch.run_poll(5)

    row = (await watch.rows())[-1]
    assert (row.head_sha, row.previous_head_sha, row.descends_from_previous) == (None, A, False)
    assert (row.lines_added, row.files_changed, row.commits) == (0, 0, [])


async def test_the_same_poll_run_twice_writes_one_row(watch: Watch) -> None:
    watch.head(None)
    await watch.run_kickoff()
    watch.head(A)
    watch.provider.comparisons[(None, A)] = ahead(10, A)

    await watch.run_poll(5)
    await watch.run_poll(5)

    assert [r.head_sha for r in await watch.rows()] == [None, A]


async def test_a_repository_that_is_gone_is_a_result_not_a_retry(watch: Watch) -> None:
    watch.provider.heads[URL] = RepoNotFound("gone")

    outcome = await watch.run_kickoff()

    assert outcome is not None and outcome.error_class == "app"
    assert await watch.rows() == []


async def test_a_provider_outage_is_left_to_the_job_loop(watch: Watch) -> None:
    watch.head(None)
    await watch.run_kickoff()
    watch.provider.heads[URL] = InfraError("GitHub could not be reached")

    with pytest.raises(InfraError):
        await watch.run_poll(5)


async def test_a_team_with_no_repository_is_a_validation_outcome(sessions: Sessions) -> None:
    watch = Watch(sessions)
    await watch.register(registered_at=None)

    for outcome in (await watch.run_kickoff(), await watch.run_poll(5)):
        assert outcome is not None and outcome.error_class == "validation"
