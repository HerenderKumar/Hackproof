"""Builders for kickoff-scan parts (M9, the plan's A4), shaped as the scanner makes them."""

import gzip
import hashlib
import io
import json
import tarfile
from collections.abc import Sequence
from dataclasses import asdict

from core.baselines.parts import Entry


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
