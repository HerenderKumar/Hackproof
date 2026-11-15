"""The `purge_baselines` job (M9, D-052): device code is deleted after the event's retention.

Each baseline's folder (its encrypted parts and its index) is deleted, then the baseline is
marked `purged`. Then, in one transaction, every match excerpt of the event's reports is
emptied and the reports say when. The numbers, the paths and the teams' explanations stay, so
a judge can still read what the report found, only not the code itself.
"""

import asyncio
import uuid

from sqlalchemy import func, select, update

from core import queue
from core.baselines.store import BaselineStore
from core.db import models
from core.db.engine import Sessions
from core.logging import get_logger
from core.settings import Settings
from worker.lanes import Handler, Outcome

log = get_logger()


def make_purge_handler(sessions: Sessions, settings: Settings) -> Handler:
    async def purge(job: queue.Job) -> Outcome | None:
        try:
            event_id = uuid.UUID(str(job.payload.get("event_id")))
        except ValueError:
            return Outcome("validation", "purge_baselines needs an event_id")
        store = BaselineStore(settings.artifact_dir, settings.baseline_cipher_key)
        async with sessions() as session:
            baselines = (
                await session.scalars(
                    select(models.Baseline.id)
                    .join(models.Team, models.Team.id == models.Baseline.team_id)
                    .where(models.Team.event_id == event_id, models.Baseline.status != "purged")
                )
            ).all()
        for baseline_id in baselines:
            await asyncio.to_thread(store.delete, baseline_id)  # a folder already gone is fine
            async with sessions.begin() as session:
                await session.execute(
                    update(models.Baseline)
                    .where(models.Baseline.id == baseline_id)
                    .values(status="purged")
                )

        async with sessions.begin() as session:
            reports = (
                await session.scalars(
                    select(models.PriorWorkReport.id)
                    .join(
                        models.Submission,
                        models.Submission.id == models.PriorWorkReport.submission_id,
                    )
                    .join(models.Team, models.Team.id == models.Submission.team_id)
                    .where(
                        models.Team.event_id == event_id,
                        models.PriorWorkReport.purged_at.is_(None),
                    )
                )
            ).all()
            if reports:
                await session.execute(
                    update(models.PriorWorkMatch)
                    .where(models.PriorWorkMatch.report_id.in_(reports))
                    .values(final_excerpt=None, old_excerpt=None)
                )
                await session.execute(
                    update(models.PriorWorkReport)
                    .where(models.PriorWorkReport.id.in_(reports))
                    .values(purged_at=func.now())
                )
        log.info("baselines_purged", baselines=len(baselines), reports=len(reports))
        return None

    return purge
