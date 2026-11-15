"""Kickoff-baseline fixtures B1 to B5 (M9, Task 11): device folders, a final repository, and
what comparing them must find.

A fixture folder holds `fixture.json` (`title`; `devices`: label to `{folder, team, late}`, the
team `own` being the one compared; `starter`: a folder or null; `make`: what the runner adds to
its copy of a device folder, each `{device, path, content}` or `{device, path, unreadable}`),
the device folders, `final/`, and `expected.json`. `make` exists because this repository's
`.gitignore` leaves out `.env` and `node_modules/`, so those cannot be committed in a fixture.
"""

import importlib.util
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from scripts.fixtures import FIXTURES_DIR, FixtureError

SCANNER_PATH = Path(__file__).resolve().parent.parent / "scanner" / "hackproof_scan.py"
OWN_TEAM = "own"
_FIXTURE_DIR = re.compile(r"^(B\d)-[a-z0-9]+(?:-[a-z0-9]+)*$")
EXPECTED_FIELDS = {
    "percent", "late_percent", "identical_files", "matches", "excluded_starter",
    "excluded_common", "scan",
}  # fmt: skip
MATCH_KINDS = {"identical_file", "unchanged", "edited", "possibly_related"}


@dataclass(frozen=True)
class Device:
    label: str
    folder: Path
    team: str
    late: bool


@dataclass(frozen=True)
class Make:
    device: str
    path: str
    content: str | None
    unreadable: bool


@dataclass(frozen=True)
class BaselineFixture:
    id: str
    title: str
    root: Path
    devices: tuple[Device, ...]
    starter: Path | None
    make: tuple[Make, ...]
    final: Path
    expected: dict[str, Any]


def load_scanner() -> ModuleType:
    """The kickoff scanner, from its file: it is a script participants run, not a package."""
    name = "hackproof_scan"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCANNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look their module up while the file runs
    spec.loader.exec_module(module)
    return module


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise FixtureError(f"{path}: {exc}") from exc


def _load(folder: Path) -> BaselineFixture:
    spec, expected = _read(folder / "fixture.json"), _read(folder / "expected.json")
    devices = tuple(
        Device(label, folder / d["folder"], d["team"], bool(d["late"]))
        for label, d in sorted(spec["devices"].items())
    )
    if not any(d.team == OWN_TEAM for d in devices):
        raise FixtureError(f"{folder.name}: no device belongs to the team {OWN_TEAM!r}")
    for device in devices:
        if not device.folder.is_dir():
            raise FixtureError(f"{folder.name}: no device folder {device.folder.name}")
    make = tuple(
        Make(m["device"], m["path"], m.get("content"), bool(m.get("unreadable", False)))
        for m in spec.get("make", [])
    )
    unknown = set(expected) - EXPECTED_FIELDS
    if unknown:
        raise FixtureError(f"{folder.name}: unknown expected fields {sorted(unknown)}")
    for match in expected.get("matches", []):
        if match.get("kind") not in MATCH_KINDS:
            raise FixtureError(f"{folder.name}: a match of unknown kind {match.get('kind')!r}")
    starter = folder / spec["starter"] if spec.get("starter") else None
    return BaselineFixture(
        id=folder.name,
        title=spec["title"],
        root=folder,
        devices=devices,
        starter=starter,
        make=make,
        final=folder / "final",
        expected=expected,
    )


def load_baseline_fixtures(root: Path = FIXTURES_DIR) -> list[BaselineFixture]:
    folder = root / "baseline"
    return [_load(p) for p in sorted(folder.iterdir()) if _FIXTURE_DIR.match(p.name)]
