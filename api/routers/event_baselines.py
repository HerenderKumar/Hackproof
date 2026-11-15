"""The organiser's side of the kickoff baseline (M9, Task 15): starter kits and every team's
devices, with what each scan could not read.

Nothing here returns a baseline's code: a starter keeps only its runs, and the feed lists
devices, counts and reasons.
"""

import asyncio
import hashlib
import uuid
from typing import Annotated

from fastapi import APIRouter, Form, Request, Response, UploadFile
from sqlalchemy import select

from api.baseline_common import LIVE, device_out
from api.baseline_schemas import (
    BaselinesFeed,
    DeviceDetail,
    ScanSummary,
    StarterOut,
    TeamBaselines,
    Unreadable,
)
from api.deps import Db, Organiser
from api.errors import api_error
from core.baselines.starter import BadStarter, StarterTooLarge, starter_files
from core.db import models
from core.priorwork.model import MatchSettings
from core.priorwork.run import starter_runs

router = APIRouter(prefix="/api", tags=["organiser"])

UPLOAD_MAX_BYTES = 20_000_000  # the archive as sent; its code is held to 5 MB below


async def _event(db: Db, event_id: uuid.UUID) -> models.Event:
    event = await db.get(models.Event, event_id)
    if event is None:
        raise api_error(404, "event_not_found", "There is no event with that ID.")
    return event


def _starter_out(starter: models.Starter) -> StarterOut:
    return StarterOut(
        id=starter.id,
        name=starter.name,
        files=starter.files,
        runs=len(starter.runs) // 8,
        created_at=starter.created_at,
    )


@router.get("/events/{event_id}/starters")
async def list_starters(event_id: uuid.UUID, _: Organiser, db: Db) -> list[StarterOut]:
    await _event(db, event_id)
    found = await db.scalars(
        select(models.Starter)
        .where(models.Starter.event_id == event_id)
        .order_by(models.Starter.created_at)
    )
    return [_starter_out(s) for s in found.all()]


def _runs(data: bytes, extensions: list[str], max_file_bytes: int) -> tuple[int, bytes]:
    files = starter_files(data, extensions, max_file_bytes)
    return len(files), starter_runs(files, MatchSettings())


@router.post("/events/{event_id}/starters", status_code=201)
async def add_starter(
    event_id: uuid.UUID,
    name: Annotated[str, Form(min_length=1, max_length=100)],
    file: UploadFile,
    _: Organiser,
    request: Request,
    response: Response,
    db: Db,
) -> StarterOut:
    """Approve a starter kit. The same archive again answers 200 with the stored one."""
    event = await _event(db, event_id)
    data = await file.read(UPLOAD_MAX_BYTES + 1)
    if len(data) > UPLOAD_MAX_BYTES:
        raise api_error(413, "file_too_large", "A starter archive is at most 20 MB.")
    digest = hashlib.sha256(data).hexdigest()
    stored = await db.scalar(
        select(models.Starter).where(
            models.Starter.event_id == event_id, models.Starter.archive_sha256 == digest
        )
    )
    if stored is not None:
        response.status_code = 200
        return _starter_out(stored)
    max_file = request.app.state.settings.baseline_max_file_bytes
    try:
        files, runs = await asyncio.to_thread(_runs, data, list(event.scan_extensions), max_file)
    except StarterTooLarge as exc:
        raise api_error(422, "starter_too_large", str(exc)) from exc
    except BadStarter as exc:
        raise api_error(422, "bad_starter", f"The starter was refused: {exc}") from exc
    starter = models.Starter(
        event_id=event_id,
        name=name.strip(),
        archive_sha256=digest,
        files=files,
        runs=runs,
        settings={k: v for k, v in MatchSettings().stored().items() if k != "w"},
    )
    db.add(starter)
    await db.commit()
    await db.refresh(starter)
    return _starter_out(starter)


@router.delete("/events/{event_id}/starters/{starter_id}", status_code=204)
async def remove_starter(
    event_id: uuid.UUID, starter_id: uuid.UUID, _: Organiser, db: Db
) -> Response:
    starter = await db.get(models.Starter, starter_id)
    if starter is None or starter.event_id != event_id:
        raise api_error(404, "starter_not_found", "This event has no such starter.")
    await db.delete(starter)
    await db.commit()
    return Response(status_code=204)


def _detail(device: models.BaselineDevice, baselines: list[models.Baseline]) -> DeviceDetail:
    live = next((b for b in baselines if b.status in LIVE), None)
    return DeviceDetail(
        **device_out(device, baselines).model_dump(),
        summary=ScanSummary.model_validate(live.summary) if live and live.summary else None,
        unreadable=[Unreadable.model_validate(u) for u in live.unreadable] if live else [],
        unreadable_more=live.unreadable_more if live else 0,
        scanner_version=live.scanner_version if live else None,
        platform=live.platform if live else None,
    )


@router.get("/events/{event_id}/baselines")
async def baselines_feed(event_id: uuid.UUID, _: Organiser, db: Db) -> BaselinesFeed:
    """Every team's devices: whose scan is missing, late or incomplete, and why."""
    await _event(db, event_id)
    teams = (
        await db.scalars(
            select(models.Team).where(models.Team.event_id == event_id).order_by(models.Team.name)
        )
    ).all()
    ids = [t.id for t in teams]
    devices = (
        await db.scalars(
            select(models.BaselineDevice)
            .where(models.BaselineDevice.team_id.in_(ids))
            .order_by(models.BaselineDevice.created_at, models.BaselineDevice.label)
        )
    ).all()
    baselines = (
        await db.scalars(select(models.Baseline).where(models.Baseline.team_id.in_(ids)))
    ).all()
    feed = []
    for team in teams:
        details = [
            _detail(d, [b for b in baselines if b.device_id == d.id])
            for d in devices
            if d.team_id == team.id
        ]
        scanned = sum(1 for d in details if d.state in ("complete", "incomplete"))
        missing = max(0, team.members - scanned) if team.members is not None else None
        feed.append(
            TeamBaselines(
                team_id=team.id,
                team_name=team.name,
                members=team.members,
                missing=missing,
                devices=details,
            )
        )
    return BaselinesFeed(teams=feed)
