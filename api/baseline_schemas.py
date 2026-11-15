"""Request and response bodies of the kickoff baseline (M9, D-052): devices, scans, receipts.

No body here carries a baseline's file contents: a device's code reaches the server only as
encrypted parts, and no route returns them.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

from api.schemas import Strict

MAX_UNREADABLE = 2000

DeviceState = Literal["missing", "uploading", "complete", "incomplete"]


class DeviceIn(Strict):
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]


class DeviceOut(BaseModel):
    id: uuid.UUID
    label: str
    state: DeviceState
    late: bool
    indexed: bool
    sealed_at: datetime | None
    files: int | None
    bytes: int | None
    incomplete_reasons: list[str]
    receipt_hash: str | None


class DevicesView(BaseModel):
    devices: list[DeviceOut]
    can_add: bool
    kickoff_at: datetime
    deadline_at: datetime
    extensions: list[str]


class DeviceCreated(BaseModel):
    """The device's code is in this response and nowhere else: only its hash is kept."""

    device: DeviceOut
    code: str
    download_url: str
    command: str


class ScanConfig(BaseModel):
    event_name: str
    team_name: str
    device_label: str
    kickoff_at: datetime
    deadline_at: datetime
    server_time: datetime
    extensions: list[str]
    max_file_bytes: int
    part_raw_bytes: int
    max_bytes: int
    retention_days: int
    will_be_late: bool
    replaces_earlier: bool


class ScanStart(Strict):
    scanner_version: Annotated[str, StringConstraints(min_length=1, max_length=20)]
    platform: Annotated[str, StringConstraints(min_length=1, max_length=100)]


class ScanStarted(BaseModel):
    baseline_id: uuid.UUID


class PartStored(BaseModel):
    n: int
    sha256: str
    bytes: int
    already_had: bool


class Unreadable(Strict):
    path: Annotated[str, StringConstraints(max_length=4300)]
    kind: Literal["file", "folder"]
    reason: Annotated[str, StringConstraints(max_length=500)]


class ScanSummary(Strict):
    roots: list[Annotated[str, StringConstraints(max_length=4096)]] = Field(max_length=64)
    files_found: int = Field(ge=0)
    bytes_found: int = Field(ge=0)
    skipped: dict[Annotated[str, StringConstraints(max_length=40)], int] = Field(max_length=20)
    keys_blanked: int = Field(ge=0)
    size_limit_reached: bool


class SealIn(Strict):
    scanner_version: Annotated[str, StringConstraints(min_length=1, max_length=20)]
    platform: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    extensions: list[str] = Field(max_length=40)
    limited_to: list[Annotated[str, StringConstraints(max_length=4096)]] | None = Field(
        default=None, max_length=64
    )
    parts: int = Field(ge=0, le=100_000)
    files: int = Field(ge=0)
    summary: ScanSummary
    unreadable: list[Unreadable] = Field(max_length=MAX_UNREADABLE)
    unreadable_more: int = Field(default=0, ge=0)


class BaselineReceipt(BaseModel):
    """What the scanner prints and saves. `receipt_hash` is the SHA-256 of the canonical JSON
    of the other fields, so whoever holds the receipt can recompute it."""

    baseline_id: str
    team_id: str
    team_name: str
    device_label: str
    sealed_at: str
    late: bool
    complete: bool
    files: int
    bytes: int
    manifest_sha256: str
    scanner_version: str
    receipt_hash: str
