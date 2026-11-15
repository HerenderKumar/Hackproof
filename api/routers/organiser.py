"""The organiser's event page (sections 5.1 and 17): the dashboard, the spot-check of a team's
tests, job rerun, the score lock and the score export.

Rerun never edits a result: it adds a job of the same kind for the same subject, and readers
take the newest job's rows. Totals in the export are computed here, never stored.
"""

import csv
import io
import uuid
from typing import Any

from fastapi import APIRouter, Response
from sqlalchemy import func, select

from api.deps import Db, Organiser
from api.errors import api_error
from api.judging import brief_status, judged_submission, totals
from api.routers.team_tests import tests_view
from core import queue
from core.brief_data import job_state, latest_job
from core.db import models
from core.results import final_run_job, submission_verdicts

router = APIRouter(prefix="/api", tags=["organiser"])
JOBS_SHOWN = 60


async def _event(db: Db, event_id: uuid.UUID) -> models.Event:
    event = await db.get(models.Event, event_id)
    if event is None:
        raise api_error(404, "event_not_found", "There is no event with that ID.")
    return event


@router.get("/events")
async def list_events(_: Organiser, db: Db) -> dict[str, Any]:
    events = await db.scalars(select(models.Event).order_by(models.Event.kickoff_at.desc()))
    return {
        "events": [
            {
                "id": str(event.id),
                "name": event.name,
                "kickoff_at": event.kickoff_at.isoformat(),
                "deadline_at": event.deadline_at.isoformat(),
                "scores_locked": event.scores_locked,
            }
            for event in events
        ]
    }


@router.get("/events/{event_id}/dashboard")
async def dashboard(event_id: uuid.UUID, _: Organiser, db: Db) -> dict[str, Any]:
    event = await _event(db, event_id)
    teams = list(
        await db.scalars(
            select(models.Team).where(models.Team.event_id == event.id).order_by(models.Team.name)
        )
    )
    names = {team.id: team.name for team in teams}
    team_rows = []
    for team in teams:
        claims_version = await db.scalar(
            select(func.max(models.Claims.version)).where(models.Claims.team_id == team.id)
        )
        last_seen = await db.scalar(
            select(func.max(models.RepoObservation.observed_at)).where(
                models.RepoObservation.team_id == team.id
            )
        )
        # An unchanged head writes no observation, so the last poll is the newest done job.
        last_polled = await db.scalar(
            select(func.max(models.Job.finished_at)).where(
                models.Job.team_id == team.id,
                models.Job.kind == "poll_repo",
                models.Job.status == "done",
            )
        )
        submission = await judged_submission(db, team, event)
        row: dict[str, Any] = {
            "id": str(team.id),
            "name": team.name,
            "repo_url": team.repo_url if team.registered_at is not None else None,
            "registered": team.registered_at is not None,
            "rules_acknowledged": team.rules_acknowledged_at is not None,
            "claims_version": claims_version,
            "last_observed_at": last_seen.isoformat() if last_seen is not None else None,
            "last_polled_at": last_polled.isoformat() if last_polled is not None else None,
            "submission": None,
        }
        if submission is not None:
            row["submission"] = {
                "id": str(submission.id),
                "submitted_at": submission.submitted_at.isoformat(),
                "final_run": job_state(await final_run_job(db, submission.id)),
                "audit": job_state(await latest_job(db, "audit", submission.id)),
                "brief": await brief_status(db, submission.id),
                "verdicts": await submission_verdicts(db, submission.id),
            }
        team_rows.append(row)

    judges = list(
        await db.scalars(
            select(models.Judge)
            .where(models.Judge.event_id == event.id)
            .order_by(models.Judge.name)
        )
    )
    names.update({judge.id: judge.name for judge in judges})
    judge_rows = []
    for judge in judges:
        opened = await db.scalar(
            select(func.count(func.distinct(models.BriefView.submission_id))).where(
                models.BriefView.judge_id == judge.id
            )
        )
        scored = await db.scalar(
            select(func.count(func.distinct(models.Score.submission_id))).where(
                models.Score.judge_id == judge.id
            )
        )
        judge_rows.append(
            {
                "id": str(judge.id),
                "name": judge.name,
                "email": judge.email,
                "briefs_opened": opened or 0,
                "submissions_scored": scored or 0,
            }
        )

    links = await db.scalars(
        select(models.AccessLink)
        .where(models.AccessLink.event_id == event.id)
        .order_by(models.AccessLink.created_at.desc())
    )
    jobs = await db.scalars(
        select(models.Job)
        .where(models.Job.team_id.in_([team.id for team in teams]))
        .order_by(models.Job.created_at.desc())
        .limit(JOBS_SHOWN)
    )
    return {
        "event": {
            "id": str(event.id),
            "name": event.name,
            "kickoff_at": event.kickoff_at.isoformat(),
            "deadline_at": event.deadline_at.isoformat(),
            "scores_locked": event.scores_locked,
            "rubric": event.rubric,
        },
        "teams": team_rows,
        "judges": judge_rows,
        "links": [
            {
                "id": str(link.id),
                "role": link.role,
                "subject_id": str(link.subject_id),
                "subject_name": names.get(link.subject_id, "?"),
                "expires_at": link.expires_at.isoformat(),
                "revoked": link.revoked_at is not None,
            }
            for link in links
        ],
        "jobs": [
            {
                "id": str(job.id),
                "kind": job.kind,
                "status": job.status,
                "team_name": names.get(job.team_id, "?") if job.team_id else None,
                "attempts": job.attempts,
                "error_class": job.error_class,
                "error_detail": job.error_detail,
                "created_at": job.created_at.isoformat(),
                "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            }
            for job in jobs
        ],
    }


@router.get("/teams/{team_id}/specs")
async def spot_check(team_id: uuid.UUID, _: Organiser, db: Db) -> dict[str, Any]:
    """A team's tests as the team sees them, plus what only the organiser sees: each test's
    status and its hidden final cases (section 5.1, step 7)."""
    team = await db.get(models.Team, team_id)
    if team is None:
        raise api_error(404, "team_not_found", "There is no team with that ID.")
    view = (await tests_view(db, team.id)).model_dump(mode="json")
    ids = [uuid.UUID(c["test"]["id"]) for c in view["claims"] if c["test"] is not None]
    specs = {
        str(spec.id): spec
        for spec in await db.scalars(select(models.TestSpec).where(models.TestSpec.id.in_(ids)))
    }
    for claim in view["claims"]:
        if claim["test"] is not None:
            spec = specs[claim["test"]["id"]]
            claim["test"].update(status=spec.status, final_cases=spec.final_cases)
    return {"team_id": str(team.id), "team_name": team.name, **view}


@router.post("/jobs/{job_id}/rerun", status_code=201)
async def rerun_job(job_id: uuid.UUID, _: Organiser, db: Db) -> dict[str, Any]:
    """Queue the same work again as a new job. The old job and its rows are not touched."""
    job = await db.get(models.Job, job_id)
    if job is None:
        raise api_error(404, "job_not_found", "There is no job with that ID.")
    if job.status not in ("done", "failed"):
        raise api_error(409, "job_not_finished", "Only a finished job can be run again.")
    base = job.idempotency_key.split(":rerun:")[0]
    earlier = await db.scalar(
        select(func.count())
        .select_from(models.Job)
        .where(models.Job.idempotency_key.like(f"{base}:rerun:%"))
    )
    new_id, _created = await queue.enqueue(
        db,
        job.kind,
        f"{base}:rerun:{(earlier or 0) + 1}",
        payload=job.payload,
        team_id=job.team_id,
        snapshot_id=job.snapshot_id,
        submission_id=job.submission_id,
    )
    await db.commit()
    return {"id": str(new_id), "kind": job.kind}


@router.post("/events/{event_id}/lock-scores")
async def lock_scores(event_id: uuid.UUID, _: Organiser, db: Db) -> dict[str, Any]:
    event = await _event(db, event_id)
    event.scores_locked = True
    await db.commit()
    return {"scores_locked": True}


@router.get("/events/{event_id}/scores.csv")
async def scores_csv(event_id: uuid.UUID, _: Organiser, db: Db) -> Response:
    """One row per judge per judged submission that the judge scored or marked."""
    event = await _event(db, event_id)
    criteria = [str(criterion["id"]) for criterion in event.rubric]
    judges = list(
        await db.scalars(
            select(models.Judge)
            .where(models.Judge.event_id == event.id)
            .order_by(models.Judge.name)
        )
    )
    teams = await db.scalars(
        select(models.Team).where(models.Team.event_id == event.id).order_by(models.Team.name)
    )
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(
        ["team", "submission_id", "commit_sha", "judge", *criteria, "judge_total",
         "claims_verified_live", "claims_not_verified"]
    )  # fmt: skip
    for team in teams:
        submission = await judged_submission(db, team, event)
        if submission is None:
            continue
        snapshot = await db.get(models.Snapshot, submission.snapshot_id)
        for judge in judges:
            scores = dict(
                (
                    await db.execute(
                        select(models.Score.criterion_id, models.Score.value).where(
                            models.Score.judge_id == judge.id,
                            models.Score.submission_id == submission.id,
                        )
                    )
                ).all()
            )
            marks = list(
                await db.scalars(
                    select(models.ClaimMark.mark).where(
                        models.ClaimMark.judge_id == judge.id,
                        models.ClaimMark.submission_id == submission.id,
                    )
                )
            )
            if not scores and not marks:
                continue
            writer.writerow(
                [
                    _cell(team.name),
                    submission.id,
                    snapshot.commit_sha if snapshot is not None else "",
                    _cell(judge.name),
                    *[scores.get(criterion, "") for criterion in criteria],
                    totals(event.rubric, scores),
                    marks.count("verified_live"),
                    marks.count("not_verified"),
                ]
            )
    return Response(
        out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="scores-{event.id}.csv"'},
    )


def _cell(text: str) -> str:
    """A name that a spreadsheet will not run as a formula."""
    return f"'{text}" if text[:1] in ("=", "+", "-", "@") else text
