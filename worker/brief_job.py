"""The `brief` job (sections 7 and 15.4): after the audit, the written part of a judge's brief.

It reads the same facts the brief page shows, asks the model for a summary, a line per claim
and two demo questions, and stores what code kept. A model answer that cannot be read stores
the template questions with `ai_status = 'failed'`: the page renders either way. Gateway
failures propagate, so the job loop retries; a brief with no row at all still renders raw.

One `briefs` row per attempt, guarded like the audit's: looked for before the model is called
and again inside the writing transaction, under the job row's lock.
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core import queue
from core.brief import raw_content, write_brief
from core.brief_data import load_facts
from core.db import models
from core.db.engine import Sessions
from core.llm import LLMClient, LLMOutputError
from core.logging import get_logger
from core.settings import Settings
from worker.lanes import Handler, Outcome

log = get_logger()


def make_brief_handler(sessions: Sessions, client: LLMClient, settings: Settings) -> Handler:
    async def brief(job: queue.Job) -> Outcome | None:
        if job.submission_id is None:
            return Outcome("validation", "brief needs a submission")
        async with sessions() as session:
            if await _stored(session, job):
                log.info("brief_skipped", reason="this attempt already stored its brief")
                return None
            facts = await load_facts(session, job.submission_id)
        if facts is None:
            return Outcome("validation", "brief needs the submission's team, snapshot and event")

        outcome: Outcome | None = None
        try:
            content = await write_brief(client, facts, model=settings.brief_model, job_id=job.id)
            status = "ok"
        except LLMOutputError as exc:
            content, status = raw_content(facts), "failed"
            outcome = Outcome("validation", f"the brief writer's answer was unusable: {exc}"[:500])

        row = models.Brief(
            submission_id=job.submission_id,
            job_id=job.id,
            attempt_id=job.attempt_id,
            content=content,
            ai_status=status,
            model=settings.brief_model,
        )
        async with sessions.begin() as session:
            await session.execute(
                select(models.Job.id).where(models.Job.id == job.id).with_for_update()
            )
            if await _stored(session, job):
                return None
            session.add(row)
        log.info("brief_stored", ai_status=status, questions_from=content["questions_from"])
        return outcome

    return brief


async def _stored(session: AsyncSession, job: queue.Job) -> bool:
    count = await session.scalar(
        select(func.count())
        .select_from(models.Brief)
        .where(models.Brief.job_id == job.id, models.Brief.attempt_id == job.attempt_id)
    )
    return bool(count)
