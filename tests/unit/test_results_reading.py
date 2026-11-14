"""Reading results (section 10): only the job's current attempt counts, and only once it is done."""

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core import queue
from core.db import models
from core.results import current_test_results, submission_verdicts
from tests.factories import (
    make_claims,
    make_event,
    make_snapshot,
    make_submission,
    make_team,
    make_test_spec,
)

Sessions = async_sessionmaker[AsyncSession]


@dataclass
class Scene:
    submission_id: uuid.UUID
    spec_ids: dict[str, uuid.UUID]
    job: queue.Job


async def scene(sessions: Sessions) -> Scene:
    async with sessions.begin() as session:
        event = make_event()
        session.add(event)
        await session.flush()
        team = make_team(event.id)
        session.add(team)
        await session.flush()
        snapshot = make_snapshot(team.id)
        session.add_all([snapshot, make_claims(team.id)])
        await session.flush()
        specs = {claim: make_test_spec(team.id, claim_id=claim) for claim in ("c1", "c2")}
        session.add_all(specs.values())
        submission = make_submission(team.id, snapshot.id)
        session.add(submission)
        await session.flush()
        await queue.enqueue(
            session, "final_run", f"final_run:{submission.id}", submission_id=submission.id
        )
        submission_id, spec_ids = submission.id, {c: s.id for c, s in specs.items()}
    async with sessions.begin() as session:
        job = await queue.claim(session, "heavy")
    assert job is not None
    return Scene(submission_id, spec_ids, job)


async def add_result(
    sessions: Sessions, s: Scene, claim: str, verdict: str, attempt: uuid.UUID
) -> None:
    async with sessions.begin() as session:
        session.add(
            models.TestResult(
                job_id=s.job.id,
                attempt_id=attempt,
                spec_id=s.spec_ids[claim],
                case_id="f1",
                verdict=verdict,
            )
        )


async def complete(sessions: Sessions, s: Scene) -> None:
    async with sessions.begin() as session:
        assert await queue.complete(session, s.job.id, s.job.attempt_id)


async def test_only_the_current_attempts_rows_count(sessions: Sessions) -> None:
    s = await scene(sessions)
    await add_result(sessions, s, "c1", "pass", s.job.attempt_id)
    await add_result(sessions, s, "c1", "fail", uuid.uuid4())  # a stale attempt
    await complete(sessions, s)

    async with sessions() as session:
        assert await submission_verdicts(session, s.submission_id) == {
            "c1": "pass",
            "c2": "unverified",
        }
        assert len(await current_test_results(session, s.job.id)) == 1


async def test_rows_of_a_job_that_is_not_done_do_not_count(sessions: Sessions) -> None:
    s = await scene(sessions)
    await add_result(sessions, s, "c1", "pass", s.job.attempt_id)

    async with sessions() as session:
        assert await current_test_results(session, s.job.id) == []
        assert await submission_verdicts(session, s.submission_id) == {
            "c1": "unverified",
            "c2": "unverified",
        }


async def test_every_claim_of_the_submission_gets_a_verdict(sessions: Sessions) -> None:
    s = await scene(sessions)
    await add_result(sessions, s, "c1", "pass", s.job.attempt_id)
    await add_result(sessions, s, "c2", "blocked", s.job.attempt_id)
    await complete(sessions, s)

    async with sessions() as session:
        assert await submission_verdicts(session, s.submission_id) == {
            "c1": "pass",
            "c2": "blocked",
        }


async def test_a_submission_without_a_final_run_is_all_unverified(sessions: Sessions) -> None:
    async with sessions.begin() as session:
        event = make_event()
        session.add(event)
        await session.flush()
        team = make_team(event.id)
        session.add(team)
        await session.flush()
        snapshot = make_snapshot(team.id)
        session.add_all([snapshot, make_claims(team.id)])
        await session.flush()
        submission = make_submission(team.id, snapshot.id)
        session.add(submission)
        await session.flush()
        submission_id = submission.id

    async with sessions() as session:
        assert await submission_verdicts(session, submission_id) == {
            "c1": "unverified",
            "c2": "unverified",
        }
