"""An uploaded part is checked before anything of it is written (M9, review focus 2)."""

import gzip
import io
import tarfile

import pytest

from core.baselines.parts import MAX_UNPACKED_BYTES, BadPart, check_part
from tests.baseline_flow import entry, make_part, pack, sha

LIMIT = 1024 * 1024


def test_a_well_formed_part_is_read() -> None:
    part = check_part(make_part({"src/a.js": b"let a = 1;", "b.py": b"x = 2"}), LIMIT)
    assert [e.path for e in part.entries] == ["b.py", "src/a.js"]
    assert part.contents == {sha(b"let a = 1;"): b"let a = 1;", sha(b"x = 2"): b"x = 2"}


def test_an_entry_may_name_content_sent_in_an_earlier_part() -> None:
    earlier = entry("old.js", b"sent before")
    part = check_part(make_part({"a.js": b"new"}, extra_entries=[earlier]), LIMIT)
    assert len(part.entries) == 2 and sha(b"sent before") not in part.contents


def test_bytes_that_are_not_a_gzip_tar_are_refused() -> None:
    for data in (b"", b"not gzip", gzip.compress(b"gzip but not a tar" * 50)):
        with pytest.raises(BadPart):
            check_part(data, LIMIT)


def test_a_part_that_unpacks_past_the_limit_is_refused() -> None:
    zeros = b"\0" * (20 * 1024 * 1024)
    bomb = pack([entry("z.js", zeros).__dict__], [(sha(zeros), zeros)])
    assert len(bomb) < 4 * 1024 * 1024
    with pytest.raises(BadPart, match="unpacks"):
        check_part(bomb, 64 * 1024 * 1024)
    assert MAX_UNPACKED_BYTES == 8 * 1024 * 1024


def test_a_part_without_a_manifest_or_with_a_malformed_one_is_refused() -> None:
    good = entry("a.js", b"a").__dict__
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        info = tarfile.TarInfo(sha(b"a"))
        info.size = 1
        tar.addfile(info, io.BytesIO(b"a"))
    cases = [
        gzip.compress(raw.getvalue()),  # no manifest at all
        pack({"not": "a list"}, []),
        pack([{**good, "size": "1"}], [(sha(b"a"), b"a")]),
        pack([{k: v for k, v in good.items() if k != "keys_blanked"}], [(sha(b"a"), b"a")]),
        pack([{**good, "path": "x" * 4097}], [(sha(b"a"), b"a")]),
        pack([{**good, "sha256_device": "abc"}], [(sha(b"a"), b"a")]),
    ]
    for data in cases:
        with pytest.raises(BadPart):
            check_part(data, LIMIT)


def _with_member(info: tarfile.TarInfo, data: bytes = b"") -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        manifest = b"[]"
        head = tarfile.TarInfo("manifest.json")
        head.size = len(manifest)
        tar.addfile(head, io.BytesIO(manifest))
        tar.addfile(info, io.BytesIO(data) if data else None)
    return gzip.compress(raw.getvalue())


def test_links_devices_and_folders_are_refused() -> None:
    name = sha(b"x")
    for kind in (
        tarfile.SYMTYPE,
        tarfile.LNKTYPE,
        tarfile.CHRTYPE,
        tarfile.DIRTYPE,
        tarfile.FIFOTYPE,
    ):
        info = tarfile.TarInfo(name)
        info.type = kind
        info.linkname = "/etc/passwd" if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ""
        with pytest.raises(BadPart, match="regular file"):
            check_part(_with_member(info), LIMIT)


def test_a_member_whose_name_is_not_its_hash_is_refused() -> None:
    for name in (sha(b"other"), "../../etc/cron.d/x", "a" * 64):
        data = pack([entry("a.js", b"a").__dict__], [(name, b"a")])
        with pytest.raises(BadPart):
            check_part(data, LIMIT)


def test_a_file_over_the_file_limit_is_refused() -> None:
    big = b"x" * 2048
    with pytest.raises(BadPart, match="limit"):
        check_part(make_part({"big.js": big}), 1024)


def test_content_no_entry_names_is_refused() -> None:
    data = pack([entry("a.js", b"a").__dict__], [(sha(b"a"), b"a"), (sha(b"b"), b"b")])
    with pytest.raises(BadPart, match="no entry"):
        check_part(data, LIMIT)
