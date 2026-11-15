"""The `prior_work` job (M9, D-052): how much of a final submission was on the team's devices.

After every final submission, the snapshot is compared with the team's indexed baselines in
the `index` lane's process pool. Other teams' baselines only decide which code is common. The
report keeps totals and one row per match; each match's two excerpts are stored encrypted,
bound to the match's own ID. The percentage is context for a judge's questions: nothing here
scores, ranks or orders teams.

One report per attempt: looked for first, and again inside the writing transaction under the
job row's lock, as the audit's and the brief's are.
"""

import uuid
from collections.abc import Callable
from concurrent.futures import Executor
from dataclasses import asdict
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core import queue
from core.baselines.crypto import encrypt
from core.db import models
from core.db.engine import Sessions
from core.logging import get_logger
from core.priorwork.model import MatchSettings, Report, match_key
from core.priorwork.related import Embedder
from core.priorwork.run import BaselineRef, CompareRequest, run_comparison
from core.settings import Settings
from worker.index_job import in_pool
from worker.lanes import Handler, NotReady, Outcome

log = get_logger()

LIVE = ("sealed", "indexed", "failed")
WAIT_FOR_INDEX_S = 30


async def _stored(session: AsyncSession, job: queue.Job) -> bool:
    found = await session.scalar(
        select(func.count()).where(
            models.PriorWorkReport.job_id == job.id,
            models.PriorWorkReport.attempt_id == job.attempt_id,
        )
    )
    return bool(found)


def _report_row(job: queue.Job, status: str, **fields: Any) -> models.PriorWorkReport:
    assert job.submission_id is not None
    return models.PriorWorkReport(
        submission_id=job.submission_id,
        job_id=job.id,
        attempt_id=job.attempt_id,
        status=status,
        **fields,
    )


def embedder_factory(settings: Settings) -> Callable[[], Embedder] | None:
    """The "possibly related" model's factory when `EMBEDDINGS=on`: imported only then, so a
    worker with it off never loads the model's libraries."""
    if settings.embeddings != "on":
        return None
    from worker.embedder import fast_embedder

    return fast_embedder


def make_prior_work_handler(
    sessions: Sessions,
    pool: Executor,
    settings: Settings,
    *,
    every_s: float = queue.EXTEND_EVERY_S,
) -> Handler:
    match_settings = MatchSettings(context_lines=settings.match_context_lines)

    async def write(job: queue.Job, rows: list[Any]) -> bool:
        """Store this attempt's rows unless it stored them already."""
        async with sessions.begin() as session:
            await session.execute(
                select(models.Job.id).where(models.Job.id == job.id).with_for_update()
            )
            if await _stored(session, job):
                return False
            for row in rows:
                session.add(row)
                await session.flush()  # the report exists before the matches that name it
        return True

    async def prior_work(job: queue.Job) -> Outcome | None:
        if job.submission_id is None:
            return Outcome("validation", "prior_work needs a submission")
        async with sessions() as session:
            if await _stored(session, job):
                log.info("prior_work_skipped", reason="this attempt already stored its report")
                return None
            submission = await session.get(models.Submission, job.submission_id)
            snapshot = (
                await session.get(models.Snapshot, submission.snapshot_id) if submission else None
            )
            team = await session.get(models.Team, submission.team_id) if submission else None
            event = await session.get(models.Event, team.event_id) if team else None
            if submission is None or snapshot is None or team is None or event is None:
                return Outcome("validation", "prior_work needs the submission, snapshot and event")
            own = (
                await session.scalars(
                    select(models.Baseline).where(
                        models.Baseline.team_id == team.id, models.Baseline.status.in_(LIVE)
                    )
                )
            ).all()
            if any(b.status == "sealed" for b in own):
                raise NotReady(WAIT_FOR_INDEX_S, "a baseline of the team is still being indexed")
            labels = dict(
                (
                    await session.execute(
                        select(models.BaselineDevice.id, models.BaselineDevice.label).where(
                            models.BaselineDevice.team_id == team.id
                        )
                    )
                ).all()
            )
            others = (
                await session.execute(
                    select(models.Baseline.id, models.Baseline.team_id)
                    .join(models.Team, models.Team.id == models.Baseline.team_id)
                    .where(
                        models.Team.event_id == event.id,
                        models.Team.id != team.id,
                        models.Baseline.status == "indexed",
                    )
                )
            ).all()
            starters = (
                await session.scalars(
                    select(models.Starter.runs).where(models.Starter.event_id == event.id)
                )
            ).all()

        if not own:
            await write(
                job, [_report_row(job, "no_baseline", detail="the team has no kickoff scan")]
            )
            log.info("prior_work_reported", status="no_baseline")
            return None
        indexed = [b for b in own if b.status == "indexed"]
        if not indexed:
            detail = "the team's baselines could not be indexed"
            await write(job, [_report_row(job, "failed", detail=detail)])
            log.info("prior_work_reported", status="failed")
            return None

        request = CompareRequest(
            archive=Path(snapshot.archive_path),
            archive_sha256=snapshot.archive_sha256,
            extensions=tuple(event.scan_extensions),
            max_file_bytes=settings.baseline_max_file_bytes,
            store_root=settings.artifact_dir,
            key=settings.baseline_cipher_key,
            own=tuple(
                BaselineRef(b.id, str(team.id), labels.get(b.device_id, "device"), bool(b.late))
                for b in sorted(indexed, key=lambda b: str(b.id))
            ),
            others=tuple(BaselineRef(bid, str(tid), "", False) for bid, tid in others),
            starter_runs=tuple(starters),
            min_teams=event.common_code_min_teams,
            settings=match_settings,
            embedder_factory=embedder_factory(settings),
            related_min_score=settings.related_min_score,
        )
        report: Report = await in_pool(
            pool, sessions, job, run_comparison, request, every_s=every_s
        )

        key = settings.baseline_cipher_key
        row = _report_row(
            job,
            "ready",
            totals=asdict(report.totals),
            files=[asdict(f) for f in report.files],
            settings={
                **match_settings.stored(),
                "min_teams": event.common_code_min_teams,
                "extensions": list(event.scan_extensions),
                "related_min_score": (
                    request.related_min_score if request.embedder_factory else None
                ),
            },
            baseline_ids=[str(b.baseline_id) for b in request.own],
        )
        row.id = uuid.uuid4()
        matches = []
        for ordinal, m in enumerate(report.matches):
            match_id = uuid.uuid4()
            matches.append(
                models.PriorWorkMatch(
                    id=match_id,
                    report_id=row.id,
                    ordinal=ordinal,
                    match_key=match_key(m),
                    kind=m.kind,
                    late=m.late,
                    tokens=m.tokens,
                    score=m.score,
                    final_path=m.final_path,
                    final_start_line=m.final_start_line,
                    final_end_line=m.final_end_line,
                    baseline_id=uuid.UUID(m.baseline_id),
                    device_label=m.device_label,
                    old_path=m.old_path,
                    old_start_line=m.old_start_line,
                    old_end_line=m.old_end_line,
                    also_in=m.also_in,
                    final_excerpt=encrypt(
                        key, m.final_excerpt.text.encode(), f"match:{match_id}:final".encode()
                    ),
                    old_excerpt=encrypt(
                        key, m.old_excerpt.text.encode(), f"match:{match_id}:old".encode()
                    ),
                    final_excerpt_line=m.final_excerpt.first_line,
                    old_excerpt_line=m.old_excerpt.first_line,
                )
            )
        if await write(job, [row, *matches]):
            t = report.totals
            log.info(
                "prior_work_reported",
                status="ready",
                files=len(report.files),
                matches=len(matches),
                eligible=t.eligible,
                matched=t.matched,
                late_matched=t.late_matched,
            )
        return None

    return prior_work
