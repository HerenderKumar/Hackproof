"""`make baselines`: scan, store, index and compare each fixture B1 to B6 (M9, Tasks 11, 21).

For each fixture, each device folder is copied into a scratch folder, `make` adds what git
cannot hold, and the copy is scanned with the scanner's own functions. The parts are stored
encrypted and indexed as the worker would, `final/` is archived as a snapshot would be, and
`run_comparison` compares them. Nothing here touches the network or the database. The result
is compared with `expected.json`; `--show` prints every match, to read against the files.
"""

import argparse
import hashlib
import io
import os
import shutil
import sys
import tarfile
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from core.baselines.index import build_index
from core.baselines.seal import incomplete_reasons
from core.baselines.store import BaselineStore
from core.db.models import SCAN_EXTENSIONS
from core.priorwork.compare import eligible
from core.priorwork.model import MatchSettings, Report
from core.priorwork.related import Embedder
from core.priorwork.run import BaselineRef, CompareRequest, run_comparison, starter_runs
from scripts.baselines import OWN_TEAM, BaselineFixture, load_baseline_fixtures, load_scanner
from scripts.fixtures import FIXTURES_DIR, IGNORED

MAX_FILE_BYTES = 1024 * 1024
MIN_TEAMS = 3
RELATED_MIN_SCORE = 0.80
SETTINGS = MatchSettings()


def _files(folder: Path) -> dict[str, bytes]:
    return {
        p.relative_to(folder).as_posix(): p.read_bytes()
        for p in sorted(folder.rglob("*"))
        if p.is_file() and not IGNORED & set(p.relative_to(folder).parts)
    }


def _archive(folder: Path, out: Path) -> tuple[Path, str]:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for name, data in _files(folder).items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    out.write_bytes(raw.getvalue())
    return out, hashlib.sha256(raw.getvalue()).hexdigest()


def _scan(copy: Path, root: Path, key: bytes, baseline: uuid.UUID) -> dict[str, Any]:
    """Scan one device copy as the scanner would, store its parts and index them."""
    scanner = load_scanner()
    store = BaselineStore(root, key)
    limits = scanner.Limits(
        frozenset(SCAN_EXTENSIONS), MAX_FILE_BYTES, scanner.PART_RAW_BYTES, 2**31
    )
    report = scanner.ScanReport(roots=[str(copy)])
    opened: list[str] = []
    real_open = scanner.open_file

    def recording(path: str) -> bytes:
        opened.append(os.path.relpath(path, copy))
        return real_open(path)  # type: ignore[no-any-return]

    setattr(scanner, "open_file", recording)  # noqa: B010 - the module is loaded from a file
    try:
        found = (
            item
            for path in scanner.walk([str(copy)], limits, report)
            if (item := scanner.take(path, limits, report)) is not None
        )
        parts = 0
        for parts, part in enumerate(scanner.pack_parts(found, limits, report), start=1):
            store.write_part(baseline, parts, part)
    finally:
        setattr(scanner, "open_file", real_open)  # noqa: B010
    if parts:
        build_index(root, key, baseline, parts, "fixture", MAX_FILE_BYTES, SETTINGS)
    reasons = incomplete_reasons(
        len(report.unreadable), report.limited_to, report.size_limit_reached
    )
    scan = asdict(report)
    scan["unreadable"] = [
        {**item, "path": os.path.relpath(item["path"], copy)} for item in report.unreadable
    ]
    return {
        **scan,
        "opened": opened,
        "complete": not reasons,
        "incomplete_reasons": reasons,
        "parts": parts,
    }


def _make(fixture: BaselineFixture, label: str, copy: Path) -> tuple[list[Path], bool]:
    """Add what `make` lists to a device copy. Returns the folders made unreadable, and
    whether every one of them really is (not as root, not on Windows)."""
    locked: list[Path] = []
    for item in fixture.make:
        if item.device != label:
            continue
        target = copy / item.path
        if item.unreadable:
            target.mkdir(parents=True, exist_ok=True)
            (target / "inside.js").write_text("const hidden = 1;\n")
            target.chmod(0)
            locked.append(target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(item.content or "")
    really = all(not os.access(path, os.R_OK) for path in locked)
    return locked, really


def run_fixture(
    fixture: BaselineFixture,
    work: Path,
    *,
    embedder_factory: Callable[[], Embedder] | None = None,
    related_min_score: float = RELATED_MIN_SCORE,
) -> tuple[Report, dict[str, Any]]:
    """The fixture's report, and each device label's scan as the scanner saw it."""
    key, root = os.urandom(32), work / "store"
    refs: list[BaselineRef] = []
    scans: dict[str, Any] = {}
    for device in fixture.devices:
        copy = work / "devices" / device.label
        shutil.copytree(device.folder, copy, ignore=shutil.ignore_patterns(*IGNORED))
        locked, really = _make(fixture, device.label, copy)
        try:
            baseline = uuid.uuid4()
            scans[device.label] = _scan(copy, root, key, baseline)
            scans[device.label]["unreadable_checked"] = really
        finally:
            for path in locked:
                path.chmod(0o755)
        if scans[device.label]["parts"]:
            refs.append(BaselineRef(baseline, device.team, device.label, device.late))

    starters: tuple[bytes, ...] = ()
    if fixture.starter is not None:
        files = [
            (path, data)
            for path, data in _files(fixture.starter).items()
            if eligible(path, len(data), SCAN_EXTENSIONS, MAX_FILE_BYTES)
        ]
        starters = (starter_runs(files, SETTINGS),)

    archive, digest = _archive(fixture.final, work / "final.tar.gz")
    request = CompareRequest(
        archive=archive,
        archive_sha256=digest,
        extensions=tuple(SCAN_EXTENSIONS),
        max_file_bytes=MAX_FILE_BYTES,
        store_root=root,
        key=key,
        own=tuple(r for r in refs if r.team == OWN_TEAM),
        others=tuple(r for r in refs if r.team != OWN_TEAM),
        starter_runs=starters,
        min_teams=MIN_TEAMS,
        settings=SETTINGS,
        embedder_factory=embedder_factory,
        related_min_score=related_min_score,
    )
    return run_comparison(request), scans


def _fake() -> tuple[Callable[[], Embedder] | None, float]:
    from tests.priorwork_fakes import FAKE_MIN_SCORE, fake_embedder

    return fake_embedder, FAKE_MIN_SCORE


def _off() -> tuple[Callable[[], Embedder] | None, float]:
    return None, RELATED_MIN_SCORE


EMBEDDERS: dict[str, Callable[[], tuple[Callable[[], Embedder] | None, float]]] = {
    "fake": _fake,
    "off": _off,
}


def summary(report: Report, work: Path) -> dict[str, Any]:
    """The report in the terms of `expected.json`; old paths are relative to the device."""
    matches = []
    for m in report.matches:
        device = work / "devices" / m.device_label
        old = os.path.relpath(m.old_path, device) if os.path.isabs(m.old_path) else m.old_path
        matches.append({"final_path": m.final_path, "kind": m.kind, "old_path": old})
    t = report.totals
    return {
        "percent": t.percent,
        "late_percent": t.late_percent,
        "identical_files": sum(1 for m in report.matches if m.kind == "identical_file"),
        "matches": matches,
        "excluded_starter": t.excluded_starter > 0,
        "excluded_common": t.excluded_common > 0,
    }


def _key(match: dict[str, Any]) -> tuple[str, str, str]:
    return (match["final_path"], match["kind"], match["old_path"])


def differences(
    fixture: BaselineFixture, report: Report, scans: dict[str, Any], work: Path
) -> list[str]:
    got = summary(report, work)
    expected = fixture.expected
    found = []
    for field in (
        "percent",
        "late_percent",
        "identical_files",
        "excluded_starter",
        "excluded_common",
    ):
        if field in expected and expected[field] != got[field]:
            found.append(f"{field}: expected {expected[field]}, got {got[field]}")
    if "matches" in expected:
        want = sorted(_key(m) for m in expected["matches"])
        have = sorted(_key(m) for m in got["matches"])
        if want != have:
            found.append(f"matches: expected {want}, got {have}")
    if "scan" in expected:
        found += _scan_differences(expected["scan"], scans)
    return found


def _scan_differences(expected: dict[str, Any], scans: dict[str, Any]) -> list[str]:
    found = []
    scan = scans[expected.get("device", "laptop")]
    if "keys_blanked" in expected and scan["keys_blanked"] != expected["keys_blanked"]:
        found.append(
            f"scan.keys_blanked: expected {expected['keys_blanked']}, got {scan['keys_blanked']}"
        )
    for name in expected.get("never_opened", []):
        if name in scan["opened"]:
            found.append(f"scan: {name} was opened")
    for kind, count in expected.get("skipped", {}).items():
        if scan["skipped"][kind] != count:
            found.append(f"scan.skipped.{kind}: expected {count}, got {scan['skipped'][kind]}")
    if not scan["unreadable_checked"]:
        print("    (the unreadable-folder check is skipped: this user reads every folder)")
        return found
    names = sorted(item["path"] for item in scan["unreadable"])
    if "unreadable" in expected and names != sorted(expected["unreadable"]):
        found.append(f"scan.unreadable: expected {expected['unreadable']}, got {names}")
    if any(not item["reason"] for item in scan["unreadable"]):
        found.append("scan.unreadable: an item has no reason")
    if "complete" in expected and scan["complete"] != expected["complete"]:
        found.append(f"scan.complete: expected {expected['complete']}, got {scan['complete']}")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", help="comma-separated fixture IDs, e.g. B1-copied,B5-scan")
    parser.add_argument("--show", action="store_true", help="print every match")
    parser.add_argument(
        "--embedder",
        choices=sorted(EMBEDDERS),
        default="fake",
        help="what scores the possibly-related pairs: the tests' bag of names (the default), "
        "the worker's model (inside the worker image), or none",
    )
    parser.add_argument("--root", type=Path, default=FIXTURES_DIR, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    wanted = set(args.only.split(",")) if args.only else None
    failed = 0
    for fixture in load_baseline_fixtures(args.root):
        if wanted and fixture.id not in wanted:
            continue
        with tempfile.TemporaryDirectory(prefix="hp-baseline-") as scratch:
            work = Path(scratch)
            factory, min_score = EMBEDDERS[args.embedder]()
            report, scans = run_fixture(
                fixture, work, embedder_factory=factory, related_min_score=min_score
            )
            got = summary(report, work)
            problems = differences(fixture, report, scans, work)
        state = "ok" if not problems else "DIFFERENT"
        print(
            f"{fixture.id:<14} {state:<9} percent {got['percent']}  late {got['late_percent']}  "
            f"matches {len(got['matches'])}  starter {got['excluded_starter']}  "
            f"common {got['excluded_common']}"
        )
        if args.show:
            for match in got["matches"]:
                print(f"    {match['kind']:<15} {match['final_path']}  <-  {match['old_path']}")
        for problem in problems:
            print(f"    {problem}")
        failed += bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
