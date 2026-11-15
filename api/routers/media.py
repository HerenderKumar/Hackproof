"""A UI case's files (M11 design, part 7): its video, its still image and its trace.

One helper and three thin routes, each a `FileResponse`, so a video can be sought: iOS Safari
will not play a video from a server that ignores `Range`. The path always comes from the stored
row and must resolve inside the artifact directory, as logs must. The content type is fixed by
the kind asked for, never guessed from the file. The trace holds the team's page, so it is
always a download and HackProof never renders it.

- A judge (and the organiser) reads the files of a final run in the judge's event.
- The organiser reads the files of any run.
- A team reads the files of its own self-checks only. There is no team route to a final run's
  files: a final run's video shows the hidden cases being typed.
"""

import asyncio
import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from api.deps import Db, Organiser, Reader, TeamUser
from api.errors import api_error
from api.team_common import own_team
from core.db import models

router = APIRouter(prefix="/api", tags=["media"])

Kind = Literal["video", "poster", "trace"]

_TYPES: dict[str, str] = {
    "video": "video/webm",
    "poster": "image/jpeg",
    "trace": "application/zip",
}
_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "private, max-age=3600",
}


def stored_path(row: models.TestResult, kind: str) -> str | None:
    """Where the row says the file is. The still image sits beside the video."""
    if kind == "video":
        return row.video_path
    if kind == "trace":
        return row.trace_path
    if kind == "poster" and row.video_path:
        return str(Path(row.video_path).with_name("poster.jpg"))
    return None


def media_file(root: Path, stored: str | None) -> Path | None:
    """The file, if it exists inside `root`; None for anything else."""
    if not stored:
        return None
    path = Path(stored).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        return None
    return path


async def serve(request: Request, row: models.TestResult | None, kind: Kind) -> FileResponse:
    root: Path = request.app.state.settings.artifact_dir
    path = None
    if row is not None:
        path = await asyncio.to_thread(media_file, root, stored_path(row, kind))
    if path is None:
        raise api_error(404, "media_not_found", "That file is not available.")
    if kind == "trace":
        return FileResponse(
            path,
            media_type=_TYPES[kind],
            headers=_HEADERS,
            filename=f"trace-{row.case_id or 'case'}.zip" if row is not None else "trace.zip",
            content_disposition_type="attachment",
        )
    return FileResponse(path, media_type=_TYPES[kind], headers=_HEADERS)


async def _current(db: Db, result_id: uuid.UUID) -> tuple[models.TestResult, models.Job] | None:
    """The result and its job, if the row belongs to the job's finished, current attempt."""
    row = await db.get(models.TestResult, result_id)
    job = await db.get(models.Job, row.job_id) if row is not None else None
    if row is None or job is None or job.status != "done" or row.attempt_id != job.attempt_id:
        return None
    return row, job


@router.get("/judge/results/{result_id}/media/{kind}")
async def judge_media(
    result_id: uuid.UUID, kind: Kind, session: Reader, db: Db, request: Request
) -> FileResponse:
    found = await _current(db, result_id)
    row = None
    if found is not None and found[1].kind == "final_run" and found[1].submission_id is not None:
        submission = await db.get(models.Submission, found[1].submission_id)
        team = await db.get(models.Team, submission.team_id) if submission is not None else None
        allowed = team is not None and (
            session.role == "organiser" or team.event_id == session.event_id
        )
        row = found[0] if allowed else None
    return await serve(request, row, kind)


@router.get("/results/{result_id}/media/{kind}")
async def organiser_media(
    result_id: uuid.UUID, kind: Kind, session: Organiser, db: Db, request: Request
) -> FileResponse:
    found = await _current(db, result_id)
    return await serve(request, found[0] if found is not None else None, kind)


@router.get("/team/self-checks/{job_id}/results/{result_id}/media/{kind}")
async def team_media(
    job_id: uuid.UUID,
    result_id: uuid.UUID,
    kind: Kind,
    session: TeamUser,
    db: Db,
    request: Request,
) -> FileResponse:
    team, _ = await own_team(db, session)
    found = await _current(db, result_id)
    row = None
    if found is not None:
        result, job = found
        own = job.kind == "self_check_run" and job.team_id == team.id and job.id == job_id
        row = result if own else None
    return await serve(request, row, kind)
