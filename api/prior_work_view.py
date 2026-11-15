"""One view of a prior-work report, shared by the team and the judge (M9, Task 17).

The report shown is the newest whose job finished with the report's own attempt, as with run
results. Explanations are keyed by submission and match key, so they outlive a rerun of the
report; the newest row for a key is the one shown, and an empty one clears it. No view holds
an excerpt: only `match_excerpts` decrypts one, for one match.
"""

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.baseline_common import team_devices
from api.baseline_schemas import (
    ExcerptOut,
    FileShareOut,
    MatchExcerpts,
    MatchOut,
    PriorWorkView,
    TotalsOut,
)
from api.errors import api_error
from core.baselines.crypto import decrypt
from core.db import models


async def current_report(
    db: AsyncSession, submission_id: uuid.UUID
) -> models.PriorWorkReport | None:
    found: models.PriorWorkReport | None = await db.scalar(
        select(models.PriorWorkReport)
        .join(models.Job, models.Job.id == models.PriorWorkReport.job_id)
        .where(
            models.PriorWorkReport.submission_id == submission_id,
            models.Job.status == "done",
            models.Job.attempt_id == models.PriorWorkReport.attempt_id,
        )
        .order_by(models.PriorWorkReport.created_at.desc())
        .limit(1)
    )
    return found


async def explanations(
    db: AsyncSession, submission_id: uuid.UUID
) -> dict[str, tuple[str, datetime]]:
    """The newest explanation per match key; an empty text means it was cleared."""
    rows = await db.execute(
        select(
            models.MatchExplanation.match_key,
            models.MatchExplanation.text,
            models.MatchExplanation.created_at,
        )
        .where(models.MatchExplanation.submission_id == submission_id)
        .order_by(models.MatchExplanation.id.desc())
    )
    newest: dict[str, tuple[str, datetime]] = {}
    for key, text, at in rows.all():
        newest.setdefault(key, (text, at))
    return {key: said for key, said in newest.items() if said[0]}


async def _pending(db: AsyncSession, submission_id: uuid.UUID) -> bool:
    waiting = await db.scalar(
        select(func.count()).where(
            models.Job.kind == "prior_work",
            models.Job.submission_id == submission_id,
            models.Job.status.in_(("queued", "running")),
        )
    )
    return bool(waiting)


async def report_view(
    db: AsyncSession,
    submission: models.Submission,
    *,
    for_team: bool,
    scores_locked: bool,
    offset: int,
    limit: int,
) -> PriorWorkView:
    report = await current_report(db, submission.id)
    devices = await team_devices(db, submission.team_id)
    said = await explanations(db, submission.id)
    whole = said.get("")
    base = {
        "submission_id": submission.id,
        "devices": devices,
        "explanation": whole[0] if whole else None,
        "explained_at": whole[1] if whole else None,
        "can_explain": for_team and not scores_locked,
    }
    if report is None:
        pending = await _pending(db, submission.id)
        return PriorWorkView(
            status="pending" if pending else "failed",
            detail=None if pending else "the report could not be made",
            totals=None, files=[], matches=[], matches_total=0, code_deleted_at=None, **base,
        )  # fmt: skip
    total = await db.scalar(
        select(func.count()).where(models.PriorWorkMatch.report_id == report.id)
    )
    rows = (
        await db.scalars(
            select(models.PriorWorkMatch)
            .where(models.PriorWorkMatch.report_id == report.id)
            .order_by(models.PriorWorkMatch.ordinal)
            .offset(offset)
            .limit(limit)
        )
    ).all()
    matches = []
    for m in rows:
        note = said.get(m.match_key)
        matches.append(
            MatchOut(
                id=m.id, match_key=m.match_key, ordinal=m.ordinal, kind=m.kind,
                late=m.late, tokens=m.tokens, score=m.score, final_path=m.final_path,
                final_start_line=m.final_start_line, final_end_line=m.final_end_line,
                device_label=m.device_label, old_path=m.old_path,
                old_start_line=m.old_start_line, old_end_line=m.old_end_line, also_in=m.also_in,
                explanation=note[0] if note else None, explained_at=note[1] if note else None,
            )
        )  # fmt: skip
    return PriorWorkView(
        status=report.status,
        detail=report.detail,
        totals=TotalsOut.model_validate(report.totals) if report.totals else None,
        files=[FileShareOut.model_validate(f) for f in report.files],
        matches=matches,
        matches_total=total or 0,
        code_deleted_at=report.purged_at,
        **base,
    )


async def match_excerpts(
    db: AsyncSession, key: bytes, match: models.PriorWorkMatch
) -> MatchExcerpts:
    report = await db.get(models.PriorWorkReport, match.report_id)
    if (
        report is None
        or report.purged_at is not None
        or match.final_excerpt is None
        or match.old_excerpt is None
    ):
        raise api_error(410, "code_deleted", "The device code behind this match has been deleted.")
    final = decrypt(key, match.final_excerpt, f"match:{match.id}:final".encode())
    old = decrypt(key, match.old_excerpt, f"match:{match.id}:old".encode())
    return MatchExcerpts(
        id=match.id,
        final=ExcerptOut(
            first_line=match.final_excerpt_line, text=final.decode("utf-8", "replace")
        ),
        old=ExcerptOut(first_line=match.old_excerpt_line, text=old.decode("utf-8", "replace")),
    )
