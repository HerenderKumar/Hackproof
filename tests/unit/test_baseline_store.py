"""Baseline parts on disk (M9, A4): encrypted, written whole, and removed whole."""

import uuid
from pathlib import Path

import pytest

from core.baselines.crypto import BaselineCorrupt
from core.baselines.store import BaselineStore
from tests.baseline_flow import make_part

KEY = bytes(range(32))


def test_a_part_is_on_disk_only_encrypted(tmp_path: Path) -> None:
    store, baseline = BaselineStore(tmp_path, KEY), uuid.uuid4()
    secret = b"const apiSecret = 'a-very-recognisable-string';"
    part = make_part({"a.js": secret})
    store.write_part(baseline, 1, part)

    assert store.read_part(baseline, 1) == part
    on_disk = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert [p.name for p in on_disk] == ["1.bin"]
    assert store.dir(baseline) == tmp_path / "baselines" / str(baseline)
    for path in on_disk:
        assert part not in path.read_bytes() and b"recognisable" not in path.read_bytes()


def test_a_part_written_twice_is_the_same_part(tmp_path: Path) -> None:
    store, baseline = BaselineStore(tmp_path, KEY), uuid.uuid4()
    part = make_part({"a.js": b"x = 1"})
    store.write_part(baseline, 1, part)
    store.write_part(baseline, 1, part)
    assert store.read_part(baseline, 1) == part
    assert len(list(store.dir(baseline).rglob("*.bin"))) == 1
    assert not list(store.dir(baseline).rglob("*.tmp*"))


def test_a_part_read_with_another_key_is_corrupt(tmp_path: Path) -> None:
    baseline = uuid.uuid4()
    BaselineStore(tmp_path, KEY).write_part(baseline, 1, make_part({"a.js": b"x"}))
    with pytest.raises(BaselineCorrupt):
        BaselineStore(tmp_path, bytes(32)).read_part(baseline, 1)
    # A part moved to another number does not decrypt either: its label names its place.
    folder = BaselineStore(tmp_path, KEY).dir(baseline) / "parts"
    (folder / "1.bin").rename(folder / "2.bin")
    with pytest.raises(BaselineCorrupt):
        BaselineStore(tmp_path, KEY).read_part(baseline, 2)


def test_deleting_a_baseline_removes_its_folder_and_nothing_else(tmp_path: Path) -> None:
    store, gone, kept = BaselineStore(tmp_path, KEY), uuid.uuid4(), uuid.uuid4()
    store.write_part(gone, 1, make_part({"a.js": b"x"}))
    store.write_part(kept, 1, make_part({"b.js": b"y"}))
    store.index_path(gone).write_bytes(b"index")

    store.delete(gone)
    store.delete(gone)  # deleting twice is fine

    assert not store.dir(gone).exists()
    assert store.read_part(kept, 1) == make_part({"b.js": b"y"})
    assert store.index_path(kept) == store.dir(kept) / "index.sqlite"
