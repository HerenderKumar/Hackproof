"""Queue helpers shared by tests: each call runs in its own committed transaction."""

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core import queue
from core.db import models

Sessions = async_sessionmaker[AsyncSession]


async def enqueue(
    sessions: Sessions, kind: str, key: str | None = None, **kwargs: Any
) -> uuid.UUID:
    async with sessions.begin() as s:
        job_id, _created = await queue.enqueue(s, kind, key or f"{kind}:{uuid.uuid4()}", **kwargs)
    return job_id


async def claim(sessions: Sessions, lane: queue.Lane) -> queue.Job | None:
    async with sessions.begin() as s:
        return await queue.claim(s, lane)


async def claimed(sessions: Sessions, kind: str, **kwargs: Any) -> queue.Job:
    """Enqueue a job and claim it."""
    await enqueue(sessions, kind, **kwargs)
    job = await claim(sessions, queue.KINDS[kind].lane)
    assert job is not None
    return job


async def job_row(sessions: Sessions, job_id: uuid.UUID) -> models.Job:
    async with sessions() as s:
        row = await s.get(models.Job, job_id)
    assert row is not None
    return row


async def all_jobs(sessions: Sessions) -> list[models.Job]:
    async with sessions() as s:
        rows = await s.execute(text("SELECT id FROM jobs ORDER BY created_at, idempotency_key"))
        return [await job_row(sessions, job_id) for job_id in rows.scalars()]


async def expire_lease(sessions: Sessions, job_id: uuid.UUID) -> None:
    """Simulate a worker that died mid-job: its lease runs out without a completion."""
    async with sessions.begin() as s:
        await s.execute(
            text("UPDATE jobs SET lease_until = now() - interval '1 second' WHERE id = :id"),
            {"id": job_id},
        )


async def make_runnable_now(sessions: Sessions, job_id: uuid.UUID) -> None:
    async with sessions.begin() as s:
        await s.execute(text("UPDATE jobs SET run_after = now() WHERE id = :id"), {"id": job_id})
