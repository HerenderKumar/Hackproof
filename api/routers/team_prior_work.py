"""A team reads its prior-work report and explains it (M9, Task 17).

Nothing opens while the team can still resubmit: every route answers 409 `report_not_open`
until the database's `now()` passes the deadline, so the report cannot steer a submission.
The report is of the judged submission (the latest made by the deadline).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from api.baseline_common import db_now
from api.baseline_schemas import ExplanationIn, ExplanationOut, MatchExcerpts, PriorWorkView
from api.deps import Db, TeamUser
from api.errors import api_error
from api.judging import judged_submission
from api.prior_work_view import current_report, match_excerpts, report_view
from api.team_common import own_team
from core.db import models

router = APIRouter(prefix="/api", tags=["team"])

Offset = Annotated[int, Query(ge=0)]
Limit = Annotated[int, Query(ge=1, le=200)]


async def _open(db: Db, session: TeamUser) -> tuple[models.Submission, models.Event]:
    team, event = await own_team(db, session)
    if await db_now(db) <= event.deadline_at:
        raise api_error(
            409, "report_not_open", "The prior-work report opens once the deadline has passed."
        )
    submission = await judged_submission(db, team, event)
    if submission is None:
        raise api_error(404, "no_submission", "Your team made no submission by the deadline.")
    return submission, event


@router.get("/team/prior-work")
async def read_report(
    session: TeamUser, db: Db, offset: Offset = 0, limit: Limit = 100
) -> PriorWorkView:
    submission, event = await _open(db, session)
    return await report_view(
        db, submission, for_team=True, scores_locked=event.scores_locked, offset=offset, limit=limit
    )


@router.get("/team/prior-work/matches/{match_id}")
async def read_match(
    match_id: uuid.UUID, session: TeamUser, request: Request, db: Db
) -> MatchExcerpts:
    submission, _ = await _open(db, session)
    match = await db.get(models.PriorWorkMatch, match_id)
    report = await db.get(models.PriorWorkReport, match.report_id) if match else None
    if match is None or report is None or report.submission_id != submission.id:
        raise api_error(404, "match_not_found", "Your team's report has no such match.")
    return await match_excerpts(db, request.app.state.settings.baseline_cipher_key, match)


@router.put("/team/prior-work/explanations")
async def explain(body: ExplanationIn, session: TeamUser, db: Db) -> ExplanationOut:
    """Say where a match's code came from, or (with match_key '') about the whole report.
    Each save is a new row; the newest is shown. An empty text clears it."""
    submission, event = await _open(db, session)
    if event.scores_locked:
        raise api_error(409, "scores_locked", "Scores are locked: explanations are closed.")
    if body.match_key:
        report = await current_report(db, submission.id)
        known = None
        if report is not None:
            known = await db.scalar(
                select(models.PriorWorkMatch.id).where(
                    models.PriorWorkMatch.report_id == report.id,
                    models.PriorWorkMatch.match_key == body.match_key,
                )
            )
        if known is None:
            raise api_error(404, "match_not_found", "The report has no match with that key.")
    row = models.MatchExplanation(
        submission_id=submission.id, match_key=body.match_key, text=body.text
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return ExplanationOut(match_key=row.match_key, text=row.text, created_at=row.created_at)
