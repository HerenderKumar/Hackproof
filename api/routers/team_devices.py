"""A team's devices for the kickoff scan (M9, D-052).

Each member's device gets its own code, shown once; the scanner reaches the server only with
that code. Adding or removing a device uses the API's clock, like the team's other gates; the
scan itself is judged by the database's (see `api.routers.scan`).
"""

import asyncio
import uuid

from fastapi import APIRouter, Request, Response
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from api.auth import hash_token, new_token
from api.baseline_common import baseline_store, device_out, team_devices
from api.baseline_schemas import DeviceCreated, DeviceIn, DevicesView
from api.deps import Db, TeamUser
from api.errors import api_error
from api.team_common import own_team
from core.db import models

router = APIRouter(prefix="/api", tags=["team"])


@router.get("/team/devices")
async def list_devices(session: TeamUser, request: Request, db: Db) -> DevicesView:
    team, event = await own_team(db, session)
    devices = await team_devices(db, team.id)
    state = request.app.state
    return DevicesView(
        devices=devices,
        can_add=len(devices) < state.settings.max_devices
        and state.clock.now() <= event.deadline_at,
        kickoff_at=event.kickoff_at,
        deadline_at=event.deadline_at,
        extensions=list(event.scan_extensions),
    )


@router.post("/team/devices", status_code=201)
async def add_device(body: DeviceIn, session: TeamUser, request: Request, db: Db) -> DeviceCreated:
    state = request.app.state
    team, event = await own_team(db, session)
    if state.clock.now() > event.deadline_at:
        raise api_error(409, "deadline_passed", "The event's deadline has passed.")
    count = await db.scalar(select(func.count()).where(models.BaselineDevice.team_id == team.id))
    if (count or 0) >= state.settings.max_devices:
        raise api_error(
            409, "too_many_devices", f"A team has at most {state.settings.max_devices} devices."
        )
    taken = await db.scalar(
        select(models.BaselineDevice.id).where(
            models.BaselineDevice.team_id == team.id,
            func.lower(models.BaselineDevice.label) == body.label.lower(),
        )
    )
    if taken is not None:
        raise api_error(409, "device_label_taken", "Your team already has a device of that name.")

    code = new_token()
    device = models.BaselineDevice(team_id=team.id, label=body.label, code_hash=hash_token(code))
    db.add(device)
    try:
        await db.commit()
    except IntegrityError as exc:
        raise api_error(
            409, "device_label_taken", "Your team already has a device of that name."
        ) from exc
    await db.refresh(device)
    base = state.settings.public_base_url.rstrip("/")
    return DeviceCreated(
        device=device_out(device, []),
        code=code,
        download_url=f"{base}/api/scanner/hackproof_scan.py",
        command=f"python3 hackproof_scan.py --server {base} --code {code}",
    )


@router.delete("/team/devices/{device_id}", status_code=204)
async def remove_device(
    device_id: uuid.UUID, session: TeamUser, request: Request, db: Db
) -> Response:
    """Remove a device that has sent no sealed scan, with any upload it left unfinished."""
    team, _ = await own_team(db, session)
    device = await db.get(models.BaselineDevice, device_id, with_for_update=True)
    if device is None or device.team_id != team.id:
        raise api_error(404, "device_not_found", "Your team has no such device.")
    baselines = (
        await db.scalars(select(models.Baseline).where(models.Baseline.device_id == device.id))
    ).all()
    if any(b.status != "uploading" for b in baselines):
        raise api_error(
            409, "device_has_baseline", "This device has a scan on record, so it stays."
        )
    await db.execute(delete(models.Baseline).where(models.Baseline.device_id == device.id))
    await db.delete(device)
    await db.commit()
    store = baseline_store(request)
    for baseline in baselines:
        await asyncio.to_thread(store.delete, baseline.id)
    return Response(status_code=204)
