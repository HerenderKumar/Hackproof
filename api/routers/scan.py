"""The kickoff scanner's routes (M9, D-052): its configuration, an upload in parts, the seal.

The scanner holds no session: each route takes the device's code in `X-Device-Code` and
reaches only that device's own baseline. Every scan rule (kickoff, deadline, late) uses the
database's `now()`, which a participant cannot move. Each part is checked before it is kept,
then stored encrypted; the seal fixes what was sent and enqueues its index.
"""

import asyncio
import hashlib
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request
from fastapi import Path as PathParam
from fastapi.responses import PlainTextResponse
from sqlalchemy import select, update

from api.baseline_common import LIVE, PART_RAW_BYTES, SCANNER_FILE, baseline_store, db_now
from api.baseline_schemas import (
    BaselineReceipt,
    PartStored,
    ScanConfig,
    ScanStart,
    ScanStarted,
    SealIn,
)
from api.deps import Db, DeviceAuth
from api.errors import api_error
from core import queue
from core.baselines.parts import BadPart, check_part
from core.baselines.seal import incomplete_reasons
from core.db import models
from core.hashing import hash_json
from core.receipts import baseline_receipt_fields, receipt_hash

router = APIRouter(prefix="/api", tags=["scanner"])

PartNumber = Annotated[int, PathParam(ge=1, le=100_000)]


@router.get("/scanner/hackproof_scan.py")
async def scanner_file() -> PlainTextResponse:
    """The whole scanner, as text: a participant can read every line before running it."""
    source = await asyncio.to_thread(SCANNER_FILE.read_text)
    return PlainTextResponse(
        source,
        media_type="text/x-python",
        headers={"Content-Disposition": 'attachment; filename="hackproof_scan.py"'},
    )


async def _lock_device(db: Db, device_id: uuid.UUID) -> None:
    await db.execute(
        select(models.BaselineDevice.id)
        .where(models.BaselineDevice.id == device_id)
        .with_for_update()
    )


async def _baselines(db: Db, device_id: uuid.UUID) -> list[models.Baseline]:
    found = await db.scalars(
        select(models.Baseline)
        .where(models.Baseline.device_id == device_id)
        .execution_options(populate_existing=True)
    )
    return list(found.all())


async def _open_baseline(
    db: Db, baseline_id: uuid.UUID, device_id: uuid.UUID, *, lock: bool = False
) -> models.Baseline:
    query = select(models.Baseline).where(models.Baseline.id == baseline_id)
    if lock:
        query = query.with_for_update()
    baseline = await db.scalar(query.execution_options(populate_existing=True))
    if baseline is None or baseline.device_id != device_id or baseline.status != "uploading":
        raise api_error(404, "baseline_not_open", "There is no open scan with that ID.")
    return baseline


def _require_open(now: Any, event: models.Event) -> None:
    if now > event.deadline_at:
        raise api_error(409, "scan_closed", "The event's deadline has passed: no scan is taken.")


def _require_first_after_kickoff(now: Any, event: models.Event, live: list[Any]) -> None:
    if live and now > event.kickoff_at:
        raise api_error(
            409,
            "already_scanned",
            "This device was scanned before kickoff; that scan stands and cannot be replaced.",
        )


@router.get("/scan/config")
async def scan_config(found: DeviceAuth, request: Request, db: Db) -> ScanConfig:
    settings = request.app.state.settings
    event = found.event
    now = await db_now(db)
    live = [b for b in await _baselines(db, found.device.id) if b.status in LIVE]
    return ScanConfig(
        event_name=event.name,
        team_name=found.team.name,
        device_label=found.device.label,
        kickoff_at=event.kickoff_at,
        deadline_at=event.deadline_at,
        server_time=now,
        extensions=list(event.scan_extensions),
        max_file_bytes=settings.baseline_max_file_bytes,
        part_raw_bytes=PART_RAW_BYTES,
        max_bytes=settings.baseline_max_bytes,
        retention_days=event.baseline_retention_days,
        will_be_late=now > event.kickoff_at,
        replaces_earlier=bool(live),
    )


@router.post("/scan/start")
async def start_scan(body: ScanStart, found: DeviceAuth, request: Request, db: Db) -> ScanStarted:
    """Open a new upload for this device. An unfinished earlier upload is dropped."""
    await _lock_device(db, found.device.id)
    now = await db_now(db)
    _require_open(now, found.event)
    baselines = await _baselines(db, found.device.id)
    _require_first_after_kickoff(now, found.event, [b for b in baselines if b.status in LIVE])
    dropped = [b.id for b in baselines if b.status == "uploading"]
    if dropped:
        await db.execute(
            update(models.Baseline)
            .where(models.Baseline.id.in_(dropped))
            .values(status="superseded")
        )
    baseline = models.Baseline(
        device_id=found.device.id,
        team_id=found.team.id,
        status="uploading",
        scanner_version=body.scanner_version,
        platform=body.platform,
    )
    db.add(baseline)
    await db.commit()
    store = baseline_store(request)
    for old in dropped:
        await asyncio.to_thread(store.delete, old)
    return ScanStarted(baseline_id=baseline.id)


async def _body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise api_error(413, "part_too_large", f"A part is at most {limit} bytes.")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise api_error(413, "part_too_large", f"A part is at most {limit} bytes.")
        chunks.append(chunk)
    return b"".join(chunks)


def _entry(parts: list[dict[str, Any]], n: int) -> dict[str, Any] | None:
    return next((p for p in parts if p["n"] == n), None)


@router.put("/scan/{baseline_id}/parts/{n}")
async def put_part(
    baseline_id: uuid.UUID,
    n: PartNumber,
    found: DeviceAuth,
    request: Request,
    db: Db,
    x_part_sha256: Annotated[str, Header(max_length=64)],
) -> PartStored:
    """One part of the upload. The same part sent again is stored once."""
    settings = request.app.state.settings
    await _open_baseline(db, baseline_id, found.device.id)
    data = await _body(request, settings.baseline_part_bytes)
    digest = hashlib.sha256(data).hexdigest()
    if digest != x_part_sha256.lower():
        raise api_error(422, "part_hash_mismatch", "The part's bytes do not match its hash.")
    try:
        await asyncio.to_thread(check_part, data, settings.baseline_max_file_bytes)
    except BadPart as exc:
        raise api_error(422, "bad_part", f"The part was refused: {exc}") from exc

    baseline = await _open_baseline(db, baseline_id, found.device.id, lock=True)
    known = _entry(baseline.parts, n)
    if known is not None:
        if known["sha256"] != digest:
            raise api_error(409, "part_conflict", f"Part {n} was already sent with other bytes.")
        return PartStored(n=n, sha256=digest, bytes=len(data), already_had=True)
    if baseline.bytes_received + len(data) > settings.baseline_max_bytes:
        raise api_error(
            409, "baseline_full", f"A device's scan is at most {settings.baseline_max_bytes} bytes."
        )
    await asyncio.to_thread(baseline_store(request).write_part, baseline_id, n, data)
    baseline.parts = [*baseline.parts, {"n": n, "sha256": digest, "bytes": len(data)}]
    baseline.bytes_received += len(data)
    await db.commit()
    return PartStored(n=n, sha256=digest, bytes=len(data), already_had=False)


@router.post("/scan/{baseline_id}/seal")
async def seal_scan(
    baseline_id: uuid.UUID, body: SealIn, found: DeviceAuth, request: Request, db: Db
) -> BaselineReceipt:
    """Fix what was sent, mark it on time or late, and queue its index."""
    event, team, device = found.event, found.team, found.device
    await _lock_device(db, device.id)
    now = await db_now(db)
    baseline = await _open_baseline(db, baseline_id, device.id, lock=True)
    sent = {p["n"]: p for p in baseline.parts}
    if set(sent) != set(range(1, body.parts + 1)):
        raise api_error(
            409, "parts_missing", f"The scan names {body.parts} parts; {len(sent)} arrived."
        )
    if sorted(e.lower() for e in body.extensions) != sorted(event.scan_extensions):
        raise api_error(
            409, "extensions_changed", "The event's extensions changed: download the scanner again."
        )
    _require_open(now, event)
    live = [b for b in await _baselines(db, device.id) if b.status in LIVE]
    _require_first_after_kickoff(now, event, live)

    if live:
        await db.execute(
            update(models.Baseline)
            .where(models.Baseline.id.in_([b.id for b in live]))
            .values(status="superseded")
        )
    reasons = incomplete_reasons(
        len(body.unreadable) + body.unreadable_more,
        body.limited_to,
        body.summary.size_limit_reached,
    )
    baseline.status = "sealed"
    baseline.sealed_at = now
    baseline.late = now > event.kickoff_at
    baseline.complete = not reasons
    baseline.incomplete_reasons = reasons
    baseline.files = body.files
    baseline.summary = body.summary.model_dump(mode="json")
    baseline.unreadable = [item.model_dump(mode="json") for item in body.unreadable]
    baseline.unreadable_more = body.unreadable_more
    baseline.scanner_version = body.scanner_version
    baseline.platform = body.platform
    baseline.extensions = body.extensions
    baseline.manifest_sha256 = hash_json(
        {
            "parts": [sent[n]["sha256"] for n in range(1, body.parts + 1)],
            "seal": body.model_dump(mode="json"),
        }
    )
    fields = baseline_receipt_fields(baseline, device, team)
    baseline.receipt_hash = receipt_hash(fields)
    await queue.enqueue(
        db,
        "index_baseline",
        queue.index_key(baseline.id),
        team_id=team.id,
        payload={"baseline_id": str(baseline.id)},
    )
    await db.commit()
    store = baseline_store(request)
    for old in live:
        await asyncio.to_thread(store.delete, old.id)
    return BaselineReceipt(**fields, receipt_hash=baseline.receipt_hash)
