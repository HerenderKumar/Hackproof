"""The `index_baseline` job (M9, D-052): a sealed scan's parts become its fingerprint index.

The work is processor-bound (tokenising every file a device sent), so it runs in the `index`
lane's process pool while the event loop keeps the job's lease alive. A part that does not
decrypt or no longer checks out is retried as infrastructure, then fails the baseline with its
reason: the team's device shows it, and the comparison leaves that baseline out.
"""

import asyncio
import uuid
from collections.abc import Callable
from concurrent.futures import Executor
from dataclasses import asdict
from typing import Any

from sqlalchemy import update

from core import queue
from core.baselines.crypto import BaselineCorrupt
from core.baselines.index import IndexSummary, build_index
from core.baselines.parts import BadPart
from core.db import models
from core.db.engine import Sessions
from core.errors import InfraError
from core.logging import get_logger
from core.priorwork.model import MatchSettings
from core.settings import Settings
from worker.lanes import Handler, Outcome

log = get_logger()


async def in_pool[T](
    pool: Executor,
    sessions: Sessions,
    job: queue.Job,
    fn: Callable[..., T],
    *args: Any,
    every_s: float = queue.EXTEND_EVERY_S,
) -> T:
    """Run `fn(*args)` in the pool, extending the job's lease every `every_s` until it ends."""
    future = asyncio.get_running_loop().run_in_executor(pool, fn, *args)
    while True:
        done, _ = await asyncio.wait({future}, timeout=every_s)
        if done:
            return future.result()
        async with sessions.begin() as session:
            await queue.extend_lease(session, job)


def _baseline_id(job: queue.Job) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(job.payload.get("baseline_id")))
    except ValueError:
        return None


def make_index_handler(
    sessions: Sessions,
    pool: Executor,
    settings: Settings,
    *,
    every_s: float = queue.EXTEND_EVERY_S,
) -> Handler:
    match = MatchSettings(context_lines=settings.match_context_lines)

    async def index(job: queue.Job) -> Outcome | None:
        baseline_id = _baseline_id(job)
        if baseline_id is None:
            return Outcome("validation", "index_baseline needs a baseline_id")
        async with sessions() as session:
            baseline = await session.get(models.Baseline, baseline_id)
        if baseline is None:
            return Outcome("validation", f"there is no baseline {baseline_id}")
        if baseline.status == "indexed":
            log.info("index_skipped", reason="already indexed")
            return None
        if baseline.status != "sealed":
            return Outcome("validation", f"the baseline is {baseline.status}, not sealed")

        try:
            summary: IndexSummary = await in_pool(
                pool, sessions, job, build_index,
                settings.artifact_dir, settings.baseline_cipher_key, baseline_id,
                len(baseline.parts), str(job.attempt_id), settings.baseline_max_file_bytes, match,
                every_s=every_s,
            )  # fmt: skip
        except (BaselineCorrupt, BadPart) as exc:
            if job.attempts < job.max_attempts:
                raise InfraError(f"the baseline's parts could not be indexed: {exc}") from exc
            detail = f"the baseline's parts could not be indexed: {exc}"[:500]
            async with sessions.begin() as session:
                await session.execute(
                    update(models.Baseline)
                    .where(models.Baseline.id == baseline_id, models.Baseline.status == "sealed")
                    .values(status="failed", error_detail=detail)
                )
            log.warning("baseline_failed", error_detail=detail)
            return Outcome("app", detail)

        async with sessions.begin() as session:
            row = await session.get(models.Baseline, baseline_id, with_for_update=True)
            if row is None or row.status != "sealed":
                log.info("index_discarded", reason="the baseline changed while it was indexed")
                return None
            row.status = "indexed"
            row.index_settings = match.stored()
            row.index_summary = asdict(summary)
            if summary.missing_content:
                count = summary.missing_content
                row.complete = False
                row.incomplete_reasons = [
                    *row.incomplete_reasons,
                    f"{count} file{'s were' if count != 1 else ' was'} listed but not uploaded",
                ]
        log.info("baseline_indexed", **asdict(summary))
        return None

    return index
