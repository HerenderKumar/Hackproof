"""Collecting a run's UI files (M11 design, part 6).

The runner shares its folder with a browser that rendered a team's page, so nothing it says
about its files is taken on trust. The worker looks only where it expects a file,
`ui/<spec_id>/<n>/<name>` for the specs and cases it sent, and keeps a file only when it is a
regular file (not a link), under its kind's size cap and the run's total, and starts with its
kind's first bytes. Kept files move out of the run folder, which is deleted when the run ends,
to `ARTIFACT_DIR/runs/<attempt>/ui/<spec_id>/<n>/`, beside the logs, until `make prune`.
"""

import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MB = 1 << 20
RUN_TOTAL_LIMIT = 150 * MB


@dataclass(frozen=True)
class Kind:
    name: str  # the file's name in a case folder
    limit: int
    magic: bytes


KINDS = {
    "video": Kind("video.webm", 25 * MB, bytes.fromhex("1a45dfa3")),  # WebM (Matroska)
    "trace": Kind("trace.zip", 40 * MB, b"PK\x03\x04"),
    "poster": Kind("poster.jpg", 1 * MB, b"\xff\xd8\xff"),
}

Media = dict[tuple[str, int], dict[str, Path]]  # (spec_id, case number) -> kind -> kept file


@dataclass
class Collected:
    media: Media = field(default_factory=dict)
    dropped: int = 0


def expected_cases(specs: list[dict[str, Any]], mode: str) -> list[tuple[str, int]]:
    """(spec_id, case number) for every case of every UI spec the run carried."""
    found = []
    for spec in specs:
        if spec.get("kind") != "ui":
            continue
        cases = spec.get(f"{mode}_cases") or []
        found += [(str(spec.get("spec_id", "")), n) for n in range(1, len(cases) + 1)]
    return found


def collect(run_dir: Path, specs: list[dict[str, Any]], mode: str, destination: Path) -> Collected:
    """Move the files that pass the checks to `destination`. Never raises for a bad file."""
    collected = Collected()
    total = 0
    root = run_dir.resolve()
    for spec_id, number in expected_cases(specs, mode):
        case_dir = run_dir / "ui" / spec_id / str(number)
        if case_dir.resolve() != root / "ui" / spec_id / str(number):
            collected.dropped += int(_exists(case_dir))  # a link on the way: nothing is read
            continue
        for kind_name, kind in KINDS.items():
            source = case_dir / kind.name
            size = _acceptable(source, kind)
            if size is None:
                collected.dropped += int(_exists(source))
                continue
            if total + size > RUN_TOTAL_LIMIT:
                collected.dropped += 1
                continue
            target = destination / spec_id / str(number) / kind.name
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                _copy_exactly(source, target, size)
            except OSError:
                collected.dropped += 1
                continue
            total += size
            collected.media.setdefault((spec_id, number), {})[kind_name] = target
    return collected


def _acceptable(path: Path, kind: Kind) -> int | None:
    """The file's size if it may be kept, else None."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size == 0 or info.st_size > kind.limit:
        return None
    try:
        with _open_no_follow(path) as handle:
            if handle.read(len(kind.magic)) != kind.magic:
                return None
    except OSError:
        return None
    return info.st_size


def _exists(path: Path) -> bool:
    try:
        os.lstat(path)
    except OSError:
        return False
    return True


def _open_no_follow(path: Path) -> Any:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    return os.fdopen(descriptor, "rb")


def _copy_exactly(source: Path, target: Path, size: int) -> None:
    """Copy at most `size` bytes through a handle that does not follow links, so a file swapped
    for a link after the check is not read through it."""
    with _open_no_follow(source) as reading, open(target, "wb") as writing:
        shutil.copyfileobj(_Limited(reading, size), writing)


class _Limited:
    def __init__(self, handle: Any, left: int) -> None:
        self.handle, self.left = handle, left

    def read(self, size: int = -1) -> bytes:
        if self.left <= 0:
            return b""
        wanted = self.left if size < 0 else min(size, self.left)
        data: bytes = self.handle.read(wanted)
        self.left -= len(data)
        return data
