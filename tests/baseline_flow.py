"""Builders for kickoff-scan parts (M9, the plan's A4), shaped as the scanner makes them."""

import gzip
import hashlib
import io
import json
import tarfile
import uuid
from collections.abc import Sequence
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text

from core.baselines.index import build_index
from core.baselines.parts import Entry
from core.baselines.store import BaselineStore
from core.priorwork.model import MatchSettings


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def entry(path: str, data: bytes, *, keys_blanked: int = 0) -> Entry:
    return Entry(path, len(data), sha(data), sha(data), keys_blanked)


def _add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def pack(manifest: object, members: Sequence[tuple[str, bytes]]) -> bytes:
    """A gzip tar: `manifest.json` first, then the members as given."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        _add(tar, "manifest.json", json.dumps(manifest).encode())
        for name, data in members:
            _add(tar, name, data)
    return gzip.compress(raw.getvalue(), mtime=0)


def make_part(files: dict[str, bytes], *, extra_entries: Sequence[Entry] = ()) -> bytes:
    """A well-formed part: an entry per file, and each distinct content once, by its hash."""
    entries = [entry(path, data) for path, data in sorted(files.items())] + list(extra_entries)
    members = {sha(data): data for data in files.values()}
    return pack([asdict(e) for e in entries], sorted(members.items()))


def archive_of(folder: Path, files: dict[str, bytes]) -> tuple[Path, str]:
    """A snapshot-shaped archive (a gzip tar of the files) and its SHA-256."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for name, data in sorted(files.items()):
            _add(tar, name, data)
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / f"{uuid.uuid4().hex}.tar.gz"
    archive.write_bytes(raw.getvalue())
    return archive, sha(raw.getvalue())


DEFAULT_SETTINGS = MatchSettings()


def stored_baseline(
    root: Path, key: bytes, files: dict[str, bytes], settings: MatchSettings = DEFAULT_SETTINGS
) -> uuid.UUID:
    """A baseline whose one part is stored and indexed under `root`, as the worker leaves it."""
    baseline = uuid.uuid4()
    BaselineStore(root, key).write_part(baseline, 1, make_part(files))
    build_index(root, key, baseline, 1, "test", 1024 * 1024, settings)
    return baseline


# --- through the API (M9, Task 12) ---------------------------------------------------------


async def add_device(flow: Any, label: str = "laptop") -> tuple[dict[str, Any], str]:
    """A device of the flow's team, and its code (shown once)."""
    response = await flow.client.post("/api/team/devices", json={"label": label})
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body["device"], body["code"]


def device_client(flow: Any, code: str) -> httpx.AsyncClient:
    """The scanner's own client: no session, only the device code."""
    client = flow.api.anonymous()
    client.headers["X-Device-Code"] = code
    return client


def seal_body(
    config: dict[str, Any], files: dict[str, bytes], parts: int, **fields: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "scanner_version": "1",
        "platform": "test",
        "extensions": config["extensions"],
        "limited_to": None,
        "parts": parts,
        "files": len(files),
        "summary": {
            "roots": ["/"],
            "files_found": len(files),
            "bytes_found": sum(len(d) for d in files.values()),
            "skipped": {"dependency": 0},
            "keys_blanked": 0,
            "size_limit_reached": False,
        },
        "unreadable": [],
        "unreadable_more": 0,
    }
    return {**body, **fields}


async def put_part(
    client: httpx.AsyncClient, baseline_id: str, n: int, data: bytes
) -> httpx.Response:
    return await client.put(
        f"/api/scan/{baseline_id}/parts/{n}",
        content=data,
        headers={"X-Part-Sha256": sha(data), "Content-Type": "application/octet-stream"},
    )


async def scan(flow: Any, code: str, files: dict[str, bytes], **seal_fields: Any) -> dict[str, Any]:
    """A whole scan through the API: config, start, one part, seal. Returns the receipt."""
    client = device_client(flow, code)
    config = (await client.get("/api/scan/config")).json()
    started = await client.post(
        "/api/scan/start", json={"scanner_version": "1", "platform": "test"}
    )
    assert started.status_code == 200, started.text
    baseline_id = started.json()["baseline_id"]
    parts = 0
    if files:
        sent = await put_part(client, baseline_id, 1, make_part(files))
        assert sent.status_code == 200, sent.text
        parts = 1
    sealed = await client.post(
        f"/api/scan/{baseline_id}/seal", json=seal_body(config, files, parts, **seal_fields)
    )
    assert sealed.status_code == 200, sealed.text
    receipt: dict[str, Any] = sealed.json()
    return receipt


async def shift_event(
    sessions: Any,
    event_id: str | uuid.UUID,
    *,
    kickoff_in: timedelta | None = None,
    deadline_in: timedelta | None = None,
) -> None:
    """Move the event's times around the database's real `now()`, which the scan rules use."""
    sets = []
    params: dict[str, Any] = {"id": str(event_id)}
    if kickoff_in is not None:
        sets.append("kickoff_at = now() + :kickoff_in")
        params["kickoff_in"] = kickoff_in
    if deadline_in is not None:
        sets.append("deadline_at = now() + :deadline_in")
        params["deadline_in"] = deadline_in
    async with sessions.begin() as session:
        await session.execute(
            text(f"UPDATE events SET {', '.join(sets)}, claims_lock_at = NULL WHERE id = :id"),
            params,
        )


# --- rows without the API (M9, Task 16) ------------------------------------------------------


async def indexed_baseline(
    sessions: Any,
    root: Path,
    key: bytes,
    team_id: uuid.UUID,
    files: dict[str, bytes],
    *,
    late: bool = False,
    label: str = "laptop",
    status: str = "indexed",
) -> Any:
    """A device and a baseline of the team, its part stored and its index built, as the
    scan routes and the `index_baseline` job leave them."""
    from core.db import models
    from tests.factories import make_baseline, make_device

    async with sessions.begin() as session:
        device = make_device(team_id, label=label)
        session.add(device)
        await session.flush()
        baseline = make_baseline(
            device.id, team_id, status, late=late, complete=True,
            parts=[{"n": 1, "sha256": "x", "bytes": 1}],
        )  # fmt: skip
        session.add(baseline)
    BaselineStore(root, key).write_part(baseline.id, 1, make_part(files))
    if status == "indexed":
        build_index(root, key, baseline.id, 1, "test", 1024 * 1024, DEFAULT_SETTINGS)
    assert isinstance(baseline, models.Baseline)
    return baseline


async def submitted(
    sessions: Any, root: Path, team_id: uuid.UUID, files: dict[str, bytes], **overrides: Any
) -> Any:
    """A snapshot whose archive holds `files`, and a final submission of it."""
    from tests.factories import make_snapshot, make_submission

    archive, digest = archive_of(root / "archives", files)
    async with sessions.begin() as session:
        snapshot = make_snapshot(team_id, archive_path=str(archive), archive_sha256=digest)
        session.add(snapshot)
        await session.flush()
        submission = make_submission(team_id, snapshot.id, **overrides)
        session.add(submission)
    return submission
