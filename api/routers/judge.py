"""The judge's routes (sections 5.3, 7 and 17): the team list, the brief, marks and scores.

A judge reads only teams of the event their link was issued for; anything else is a 404, never
a 403, so IDs from another event tell them nothing. The organiser may read a brief too, without
a view being recorded. Marks and scores are the judge's own rows; scores freeze when the
organiser locks them.
"""

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import Field
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from api.auth import Session
from api.deps import Db, JudgeUser, Reader
from api.errors import api_error
from api.judging import assemble_brief, brief_status, judged_submission, totals
from api.schemas import Strict
from core.db import models
from core.results import submission_verdicts

router = APIRouter(prefix="/api", tags=["judge"])


class MarkIn(Strict):
    claim_id: str = Field(min_length=1, max_length=40)
    mark: Literal["verified_live", "not_verified", "none"]


class ScoresIn(Strict):
    scores: dict[str, int] = Field(min_length=1, max_length=20)


async def _readable(
    db: Db, session: Session, submission_id: uuid.UUID
) -> tuple[models.Submission, models.Team, models.Event]:
    """The submission with its team and event, if this caller's event holds it."""
    submission = await db.get(models.Submission, submission_id)
    team = await db.get(models.Team, submission.team_id) if submission is not None else None
    event = await db.get(models.Event, team.event_id) if team is not None else None
    wrong_event = session.role == "judge" and (event is None or event.id != session.event_id)
    if submission is None or team is None or event is None or wrong_event:
        raise api_error(404, "submission_not_found", "There is no such submission.")
    return submission, team, event


async def _own(db: Db, judge_id: uuid.UUID, submission_id: uuid.UUID) -> dict[str, Any]:
    marks = await db.execute(
        select(models.ClaimMark.claim_id, models.ClaimMark.mark).where(
            models.ClaimMark.judge_id == judge_id,
            models.ClaimMark.submission_id == submission_id,
        )
    )
    scores = await db.execute(
        select(models.Score.criterion_id, models.Score.value).where(
            models.Score.judge_id == judge_id, models.Score.submission_id == submission_id
        )
    )
    return {"marks": dict(marks.all()), "scores": dict(scores.all())}


@router.get("/judge/teams")
async def judge_teams(session: JudgeUser, db: Db) -> dict[str, Any]:
    assert session.event_id is not None and session.subject_id is not None
    event = await db.get(models.Event, session.event_id)
    if event is None:
        raise api_error(404, "event_not_found", "The event no longer exists.")
    teams = await db.scalars(
        select(models.Team).where(models.Team.event_id == event.id).order_by(models.Team.name)
    )
    rows = []
    for team in teams:
        submission = await judged_submission(db, team, event)
        row: dict[str, Any] = {"team_id": str(team.id), "team_name": team.name, "submission": None}
        if submission is not None:
            own = await _own(db, session.subject_id, submission.id)
            row["submission"] = {
                "id": str(submission.id),
                "submitted_at": submission.submitted_at.isoformat(),
                "verdicts": await submission_verdicts(db, submission.id),
                "brief": await brief_status(db, submission.id),
                "scored": len(own["scores"]),
                "total": totals(event.rubric, own["scores"]),
            }
        rows.append(row)
    return {
        "event": {"id": str(event.id), "name": event.name, "scores_locked": event.scores_locked},
        "criteria": len(event.rubric),
        "teams": rows,
    }


@router.get("/judge/submissions/{submission_id}/brief")
async def read_brief(submission_id: uuid.UUID, session: Reader, db: Db) -> dict[str, Any]:
    submission, _, event = await _readable(db, session, submission_id)
    brief = await assemble_brief(db, submission.id)
    if brief is None:
        raise api_error(404, "submission_not_found", "There is no such submission.")
    own: dict[str, Any] = {"marks": {}, "scores": {}}
    if session.role == "judge" and session.subject_id is not None:
        db.add(models.BriefView(judge_id=session.subject_id, submission_id=submission.id))
        await db.commit()
        own = await _own(db, session.subject_id, submission.id)
    return {
        **brief,
        "rubric": event.rubric,
        "scores_locked": event.scores_locked,
        "can_score": session.role == "judge",
        "own": {**own, "total": totals(event.rubric, own["scores"])},
    }


@router.put("/judge/submissions/{submission_id}/claim-marks")
async def mark_claim(
    submission_id: uuid.UUID, body: MarkIn, session: JudgeUser, request: Request, db: Db
) -> dict[str, Any]:
    assert session.subject_id is not None
    submission, team, _ = await _readable(db, session, submission_id)
    claims = await db.scalar(
        select(models.Claims).where(
            models.Claims.team_id == team.id, models.Claims.version == submission.claims_version
        )
    )
    known = {str(item.get("id")) for item in (claims.items if claims is not None else [])}
    if body.claim_id not in known:
        raise api_error(404, "claim_not_found", "This submission has no such claim.")
    key = {
        "judge_id": session.subject_id,
        "submission_id": submission.id,
        "claim_id": body.claim_id,
    }
    if body.mark == "none":
        await db.execute(delete(models.ClaimMark).filter_by(**key))
    else:
        now = request.app.state.clock.now()
        await db.execute(
            insert(models.ClaimMark)
            .values(**key, mark=body.mark, updated_at=now)
            .on_conflict_do_update(
                index_elements=["judge_id", "submission_id", "claim_id"],
                set_={"mark": body.mark, "updated_at": now},
            )
        )
    await db.commit()
    return await _own(db, session.subject_id, submission.id)


@router.put("/judge/submissions/{submission_id}/scores")
async def save_scores(
    submission_id: uuid.UUID, body: ScoresIn, session: JudgeUser, request: Request, db: Db
) -> dict[str, Any]:
    assert session.subject_id is not None
    submission, _, event = await _readable(db, session, submission_id)
    if event.scores_locked:
        raise api_error(409, "scores_locked", "The organiser has locked the scores.")
    limits = {str(criterion["id"]): int(criterion["max"]) for criterion in event.rubric}
    problems = [
        f"{criterion}: not in the rubric"
        if criterion not in limits
        else f"{criterion}: must be between 0 and {limits[criterion]}"
        for criterion, value in body.scores.items()
        if criterion not in limits or not 0 <= value <= limits[criterion]
    ]
    if problems:
        raise api_error(422, "scores_invalid", "Some scores cannot be saved.", errors=problems)
    now = request.app.state.clock.now()
    for criterion, value in body.scores.items():
        await db.execute(
            insert(models.Score)
            .values(
                judge_id=session.subject_id,
                submission_id=submission.id,
                criterion_id=criterion,
                value=value,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=["judge_id", "submission_id", "criterion_id"],
                set_={"value": value, "updated_at": now},
            )
        )
    await db.commit()
    own = await _own(db, session.subject_id, submission.id)
    return {**own, "total": totals(event.rubric, own["scores"])}
