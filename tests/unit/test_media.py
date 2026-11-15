"""Collecting a run's UI files (M11 design, part 6): only what was expected, checked, and moved."""

import os
from pathlib import Path
from typing import Any

import pytest

from worker import media
from worker.media import collect

WEBM = bytes.fromhex("1a45dfa3") + b"x" * 100
ZIP = b"PK\x03\x04" + b"x" * 100
JPEG = b"\xff\xd8\xff" + b"x" * 100
SPECS: list[dict[str, Any]] = [
    {"spec_id": "u1", "kind": "ui", "final_cases": [{"id": "f1"}, {"id": "f2"}]},
    {"spec_id": "a1", "kind": "api", "final_cases": [{"id": "f1"}]},
]


def case(run: Path, spec_id: str = "u1", number: int = 1) -> Path:
    folder = run / "ui" / spec_id / str(number)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@pytest.fixture
def run(tmp_path: Path) -> Path:
    path = tmp_path / "run"
    path.mkdir()
    return path


def test_good_files_are_moved_by_spec_and_case(run: Path, tmp_path: Path) -> None:
    for number in (1, 2):
        folder = case(run, number=number)
        (folder / "video.webm").write_bytes(WEBM)
        (folder / "trace.zip").write_bytes(ZIP)
        (folder / "poster.jpg").write_bytes(JPEG)

    collected = collect(run, SPECS, "final", tmp_path / "kept")

    assert collected.dropped == 0
    assert sorted(collected.media) == [("u1", 1), ("u1", 2)]
    assert collected.media[("u1", 2)]["trace"] == tmp_path / "kept" / "u1" / "2" / "trace.zip"
    assert collected.media[("u1", 2)]["trace"].read_bytes() == ZIP


def test_a_file_of_the_wrong_kind_is_dropped(run: Path, tmp_path: Path) -> None:
    (case(run) / "video.webm").write_bytes(b"<html>" + WEBM)

    collected = collect(run, SPECS, "final", tmp_path / "kept")

    assert (collected.media, collected.dropped) == ({}, 1)


def test_an_oversized_or_empty_file_is_dropped(
    run: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(media.KINDS, "poster", media.Kind("poster.jpg", 50, JPEG[:3]))
    (case(run) / "poster.jpg").write_bytes(JPEG)
    (case(run) / "video.webm").write_bytes(b"")

    collected = collect(run, SPECS, "final", tmp_path / "kept")

    assert (collected.media, collected.dropped) == ({}, 2)


def test_a_link_is_never_followed(run: Path, tmp_path: Path) -> None:
    secret = tmp_path / "other-team.webm"
    secret.write_bytes(WEBM)
    os.symlink(secret, case(run) / "video.webm")

    collected = collect(run, SPECS, "final", tmp_path / "kept")

    assert (collected.media, collected.dropped) == ({}, 1)


def test_a_linked_folder_on_the_way_is_not_read(run: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere" / "u1" / "1"
    elsewhere.mkdir(parents=True)
    (elsewhere / "video.webm").write_bytes(WEBM)
    os.symlink(tmp_path / "elsewhere", run / "ui")

    collected = collect(run, SPECS, "final", tmp_path / "kept")

    assert collected.media == {}


def test_only_expected_cases_are_looked_at(run: Path, tmp_path: Path) -> None:
    (case(run, "a1") / "video.webm").write_bytes(WEBM)  # an API spec has no UI files
    (case(run, number=3) / "video.webm").write_bytes(WEBM)  # u1 has two final cases

    assert collect(run, SPECS, "final", tmp_path / "kept").media == {}


def test_the_run_total_caps_what_is_kept(
    run: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(media, "RUN_TOTAL_LIMIT", len(WEBM) + 10)
    for number in (1, 2):
        (case(run, number=number) / "video.webm").write_bytes(WEBM)

    collected = collect(run, SPECS, "final", tmp_path / "kept")

    assert (list(collected.media), collected.dropped) == ([("u1", 1)], 1)
