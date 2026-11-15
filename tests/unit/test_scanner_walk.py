"""The kickoff scanner on a device (M9, Task 10): what it walks, opens, skips and packs."""

import ast
import base64
import os
import random
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.baselines.parts import check_part
from core.priorwork.model import KEY_MARKER
from tests.scanner_module import PATH, load

scan = load()
LIMITS = scan.Limits(frozenset({".js", ".py", ".ts"}), 1024 * 1024, scan.PART_RAW_BYTES, 2**31)


def tree(root: Path, files: dict[str, bytes]) -> Path:
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def run(root: Path, limits: Any = LIMITS) -> tuple[Any, list[Any]]:
    report = scan.ScanReport(roots=[str(root)])
    found = [
        f for p in scan.walk([str(root)], limits, report) if (f := scan.take(p, limits, report))
    ]
    return report, found


def names(root: Path, found: list[Any]) -> list[str]:
    return sorted(os.path.relpath(f.path, root) for f in found)


def test_only_files_on_the_list_are_taken_whatever_their_letter_case(tmp_path: Path) -> None:
    tree(tmp_path, {n: b"x = 1\n" for n in ("a.js", "B.PY", "c.Ts", "d.txt", "e.json", "f")})
    report, found = run(tmp_path)
    assert names(tmp_path, found) == ["B.PY", "a.js", "c.Ts"]
    assert (report.files_found, report.bytes_found) == (3, 18)


def test_only_files_on_the_list_are_ever_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree(tmp_path, {".env": b"SECRET=1", "notes.txt": b"n", "id_rsa": b"k", "src/a.js": b"a"})
    opened: list[str] = []
    real = scan.open_file

    def recording(path: str) -> bytes:
        opened.append(path)
        return real(path)  # type: ignore[no-any-return]

    monkeypatch.setattr(scan, "open_file", recording)
    run(tmp_path)
    assert [os.path.relpath(p, tmp_path) for p in opened] == [os.path.join("src", "a.js")]


def test_dependency_build_and_version_control_folders_are_skipped_and_counted(
    tmp_path: Path,
) -> None:
    tree(
        tmp_path,
        {
            "node_modules/x.js": b"1",
            "vendor/v.py": b"1",
            "dist/y.js": b"1",
            "target/t.js": b"1",
            ".git/z.js": b"1",
            "src/ok.js": b"1",
        },
    )
    report, found = run(tmp_path)
    assert names(tmp_path, found) == [os.path.join("src", "ok.js")]
    assert {k: v for k, v in report.skipped.items() if v} == {"dependency": 2, "build": 2, "vcs": 1}


def test_a_virtual_environment_is_known_by_its_pyvenv_cfg(tmp_path: Path) -> None:
    tree(tmp_path, {"env/pyvenv.cfg": b"home = /usr", "env/lib/x.py": b"1", "app.py": b"1"})
    report, found = run(tmp_path)
    assert names(tmp_path, found) == ["app.py"]
    assert report.skipped["dependency"] == 1


def test_links_are_never_followed(tmp_path: Path) -> None:
    root = tree(tmp_path / "root", {"a.js": b"a", "sub/b.js": b"b"})
    outside = tree(tmp_path / "outside", {"secret.js": b"s"})
    (root / "copy.js").symlink_to(root / "a.js")
    (root / "sub" / "loop").symlink_to(root, target_is_directory=True)
    (root / "out").symlink_to(outside, target_is_directory=True)

    report, found = run(root)

    assert names(root, found) == ["a.js", os.path.join("sub", "b.js")]
    assert report.skipped["link"] == 3


def test_a_file_over_the_limit_is_counted_not_taken(tmp_path: Path) -> None:
    tree(tmp_path, {"big.js": b"x" * 1001, "ok.js": b"x" * 1000})
    limits = scan.Limits(LIMITS.extensions, 1000, scan.PART_RAW_BYTES, 2**31)
    report, found = run(tmp_path, limits)
    assert names(tmp_path, found) == ["ok.js"] and report.skipped["too_large"] == 1


def test_a_binary_file_with_a_listed_extension_is_not_taken(tmp_path: Path) -> None:
    tree(tmp_path, {"bin.js": b"MZ\0\1\2", "late.js": b"x" * 9000 + b"\0"})
    report, found = run(tmp_path)
    assert names(tmp_path, found) == ["late.js"]  # a NUL past the first 8192 bytes is text
    assert report.skipped["not_text"] == 1


def test_a_cloud_placeholder_is_counted_and_never_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree(tmp_path, {"cloud.js": b"in iCloud", "here.js": b"local"})
    opened: list[str] = []
    real_open, real_stat = scan.open_file, scan.stat_of

    def stat_of(path: str) -> Any:
        info = real_stat(path)
        if path.endswith("cloud.js"):
            return SimpleNamespace(
                st_size=info.st_size, st_mode=info.st_mode, st_flags=scan.MAC_DATALESS
            )
        return info

    def recording(path: str) -> bytes:
        opened.append(os.path.basename(path))
        return real_open(path)  # type: ignore[no-any-return]

    monkeypatch.setattr(scan, "stat_of", stat_of)
    monkeypatch.setattr(scan, "open_file", recording)
    report, found = run(tmp_path)

    assert names(tmp_path, found) == ["here.js"] and opened == ["here.js"]
    assert report.skipped["cloud_placeholder"] == 1
    onedrive = SimpleNamespace(st_file_attributes=scan.WINDOWS_RECALL_ON_DATA_ACCESS)
    assert scan.is_placeholder(onedrive) and not scan.is_placeholder(SimpleNamespace())


needs_permissions = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads everything"
)


@needs_permissions
def test_an_unreadable_file_is_reported_only_with_a_listed_extension(tmp_path: Path) -> None:
    tree(tmp_path, {"locked.js": b"a", "locked.txt": b"b"})
    for name in ("locked.js", "locked.txt"):
        (tmp_path / name).chmod(0)
    try:
        report, found = run(tmp_path)
    finally:
        for name in ("locked.js", "locked.txt"):
            (tmp_path / name).chmod(0o644)
    assert found == []
    [item] = report.unreadable
    assert item["path"].endswith("locked.js") and item["kind"] == "file" and item["reason"]


@needs_permissions
def test_an_unreadable_folder_is_reported_unless_it_is_on_the_skip_list(tmp_path: Path) -> None:
    tree(tmp_path, {"locked/a.js": b"a", "node_modules/b.js": b"b", "ok.js": b"c"})
    for name in ("locked", "node_modules"):
        (tmp_path / name).chmod(0)
    try:
        report, found = run(tmp_path)
    finally:
        for name in ("locked", "node_modules"):
            (tmp_path / name).chmod(0o755)
    assert names(tmp_path, found) == ["ok.js"]
    assert [(i["path"].endswith("locked"), i["kind"]) for i in report.unreadable] == [
        (True, "folder")
    ]


def test_a_name_or_content_that_is_not_utf8_is_still_taken(tmp_path: Path) -> None:
    content = "x = 'caf\xe9';\n".encode("latin-1")
    tree(tmp_path, {"latin.js": content})
    odd_name = os.path.join(os.fsencode(tmp_path), b"caf\xe9.js")
    try:
        with open(odd_name, "wb") as handle:
            handle.write(b"y = 1;\n")
        made_odd_name = True
    except OSError:
        made_odd_name = False  # APFS refuses names that are not UTF-8

    report, found = run(tmp_path)

    by_name = {os.path.basename(f.path): f for f in found}
    assert by_name["latin.js"].data == content
    if made_odd_name:
        assert "caf\\xe9.js" in by_name
    assert report.unreadable == []


def _parts(found: list[Any], limits: Any = LIMITS) -> tuple[Any, list[bytes]]:
    report = scan.ScanReport()
    return report, list(scan.pack_parts(found, limits, report))


def test_the_parts_are_ones_the_server_accepts(tmp_path: Path) -> None:
    rng = random.Random(7)
    files = {}
    for n in range(10):
        noise = base64.encodebytes(rng.randbytes(450_000))  # about 600 KB of text
        files[f"noise/{n}.js"] = noise
    tree(tmp_path, files)
    _, found = run(tmp_path)
    assert sum(f.size for f in found) > 6_000_000

    _, parts = _parts(found)

    assert len(parts) >= 2
    entries = 0
    for part in parts:
        assert len(part) <= 4 * 1024 * 1024
        entries += len(check_part(part, LIMITS.max_file_bytes).entries)
    assert entries == 10


def test_one_content_under_two_paths_is_sent_once(tmp_path: Path) -> None:
    tree(tmp_path, {"a/x.js": b"same code", "b/x.js": b"same code", "c.js": b"other"})
    _, found = run(tmp_path)
    _, parts = _parts(found)
    checked = [check_part(p, LIMITS.max_file_bytes) for p in parts]
    assert sum(len(c.entries) for c in checked) == 3
    assert sum(len(c.contents) for c in checked) == 2


def test_packing_stops_at_the_size_limit(tmp_path: Path) -> None:
    tree(tmp_path, {f"{n}.js": bytes([97 + n]) * 1000 for n in range(3)})
    _, found = run(tmp_path)
    limits = scan.Limits(LIMITS.extensions, LIMITS.max_file_bytes, scan.PART_RAW_BYTES, 1500)
    report, parts = _parts(found, limits)
    assert report.size_limit_reached is True
    [part] = parts
    assert len(check_part(part, LIMITS.max_file_bytes).contents) == 1


def test_the_marker_is_the_one_the_comparison_knows() -> None:
    assert scan.KEY_MARKER == KEY_MARKER


def test_the_scanner_is_python_3_9_and_standard_library_only() -> None:
    source = PATH.read_text()
    module = ast.parse(source, feature_version=(3, 9))
    first = module.body[1]  # after the docstring
    assert isinstance(first, ast.ImportFrom) and first.module == "__future__"
    assert [a.name for a in first.names] == ["annotations"]
    imported = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= sys.stdlib_module_names, imported - sys.stdlib_module_names
