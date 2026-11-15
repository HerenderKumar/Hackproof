"""Judges and the organiser read a submission's prior-work report (M9, Task 17).

The percentage is context for questions, beside each match's old and new code: it is never a
score, and no response orders teams by it.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request

from api.auth import Session
from api.baseline_schemas import MatchExcerpts, PriorWorkView
from api.deps import Db, Reader
from api.errors import api_error
from api.prior_work_view import match_excerpts, report_view
from core.db import models

router = APIRouter(prefix="/api", tags=["judge"])

Offset = Annotated[int, Query(ge=0)]
Limit = Annotated[int, Query(ge=1, le=200)]


async def _readable(
    db: Db, session: Session, submission_id: uuid.UUID
) -> tuple[models.Submission, models.Event] | None:
    """The submission and its event, if this caller's event holds it (a judge reads only their
    own event; the organiser reads every one)."""
    submission = await db.get(models.Submission, submission_id)
    team = await db.get(models.Team, submission.team_id) if submission is not None else None
    event = await db.get(models.Event, team.event_id) if team is not None else None
    wrong_event = session.role == "judge" and (event is None or event.id != session.event_id)
    if submission is None or event is None or wrong_event:
        return None
    return submission, event


@router.get("/judge/submissions/{submission_id}/prior-work")
async def read_report(
    submission_id: uuid.UUID, session: Reader, db: Db, offset: Offset = 0, limit: Limit = 100
) -> PriorWorkView:
    found = await _readable(db, session, submission_id)
    if found is None:
        raise api_error(404, "submission_not_found", "There is no such submission.")
    submission, event = found
    return await report_view(
        db, submission, for_team=False, scores_locked=event.scores_locked,
        offset=offset, limit=limit,
    )  # fmt: skip


@router.get("/judge/prior-work/matches/{match_id}")
async def read_match(
    match_id: uuid.UUID, session: Reader, request: Request, db: Db
) -> MatchExcerpts:
    match = await db.get(models.PriorWorkMatch, match_id)
    report = await db.get(models.PriorWorkReport, match.report_id) if match else None
    if (
        match is None
        or report is None
        or await _readable(db, session, report.submission_id) is None
    ):
        raise api_error(404, "match_not_found", "There is no such match.")
    return await match_excerpts(db, request.app.state.settings.baseline_cipher_key, match)
