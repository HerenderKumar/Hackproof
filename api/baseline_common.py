"""What the device, scan and baseline routes share (M9, D-052)."""

import uuid
from datetime import datetime
from pathlib import Path

from fastapi import Request
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from api.baseline_schemas import DeviceOut, DeviceState
from core.baselines.store import BaselineStore
from core.db import models

# A part's unpacked size stays under this, so that even bytes that do not compress are sent in
# under BASELINE_PART_BYTES (4 MiB). The scanner closes a part before passing it.
PART_RAW_BYTES = 3_500_000
LIVE = ("sealed", "indexed", "failed")
SCANNER_FILE = Path(__file__).resolve().parent.parent / "scanner" / "hackproof_scan.py"


def baseline_store(request: Request) -> BaselineStore:
    settings = request.app.state.settings
    return BaselineStore(settings.artifact_dir, settings.baseline_cipher_key)


async def db_now(db: AsyncSession) -> datetime:
    """The database's time: every scan rule (kickoff, deadline, late) is judged by it."""
    now: datetime = await db.scalar(text("SELECT now()"))
    return now


def device_out(device: models.BaselineDevice, baselines: list[models.Baseline]) -> DeviceOut:
    """A device as its team reads it, from the device's baselines."""
    live = next((b for b in baselines if b.status in LIVE), None)
    uploading = any(b.status == "uploading" for b in baselines)
    state: DeviceState = "missing"
    if live is not None:
        state = "complete" if live.complete else "incomplete"
    elif uploading:
        state = "uploading"
    return DeviceOut(
        id=device.id,
        label=device.label,
        state=state,
        late=bool(live and live.late),
        indexed=live is not None and live.status == "indexed",
        sealed_at=live.sealed_at if live else None,
        files=live.files if live else None,
        bytes=live.bytes_received if live else None,
        incomplete_reasons=list(live.incomplete_reasons) if live else [],
        receipt_hash=live.receipt_hash if live else None,
    )


async def team_devices(db: AsyncSession, team_id: uuid.UUID) -> list[DeviceOut]:
    devices = (
        await db.scalars(
            select(models.BaselineDevice)
            .where(models.BaselineDevice.team_id == team_id)
            .order_by(models.BaselineDevice.created_at, models.BaselineDevice.label)
        )
    ).all()
    baselines = (
        await db.scalars(select(models.Baseline).where(models.Baseline.team_id == team_id))
    ).all()
    return [device_out(d, [b for b in baselines if b.device_id == d.id]) for d in devices]
