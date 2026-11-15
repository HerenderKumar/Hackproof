"""One SQLite index of fingerprints per baseline (M9, the plan's A4 and Task 9)."""

import sqlite3
import uuid
from pathlib import Path

import pytest

from core.baselines.crypto import BaselineCorrupt
from core.baselines.index import IndexedBaseline, build_index, holders
from core.baselines.parts import Entry
from core.baselines.store import BaselineStore
from core.priorwork.compare import compare
from core.priorwork.fingerprint import run_hashes, winnow
from core.priorwork.lexer import tokenize
from core.priorwork.model import MatchSettings
from tests.baseline_flow import entry, make_part, pack, sha
from tests.priorwork_fakes import MemoryBaseline, block, nobody, source

KEY = bytes(range(32))
S = MatchSettings()
LIMIT = 1024 * 1024


def _store(tmp_path: Path) -> BaselineStore:
    return BaselineStore(tmp_path, KEY)


def _build(tmp_path: Path, *parts: bytes, baseline: uuid.UUID | None = None) -> uuid.UUID:
    baseline = baseline or uuid.uuid4()
    store = _store(tmp_path)
    for n, part in enumerate(parts, start=1):
        store.write_part(baseline, n, part)
    build_index(tmp_path, KEY, baseline, len(parts), "a1", LIMIT, S)
    return baseline


def _open(tmp_path: Path, baseline: uuid.UUID, late: bool = False) -> IndexedBaseline:
    return IndexedBaseline(_store(tmp_path), baseline, device_label="laptop", late=late)


def _prints(text: str, ext: str = ".js") -> set[int]:
    syms = [t.sym for t in tokenize(text, ext)]
    return {h for _, h in winnow(run_hashes(syms, S.k), S.w)}


def test_the_index_lists_files_contents_and_fingerprints(tmp_path: Path) -> None:
    a, b = block(1).encode(), block(2).encode()
    baseline = uuid.uuid4()
    store = _store(tmp_path)
    store.write_part(baseline, 1, make_part({"src/a.js": a, "b.py": b"x = 1\n"}))
    store.write_part(baseline, 2, make_part({"src/b.js": b}))

    summary = build_index(tmp_path, KEY, baseline, 2, "a1", LIMIT, S)

    assert (summary.files, summary.contents, summary.missing_content) == (3, 3, 0)
    assert summary.tokens > 0 and summary.seconds >= 0
    assert summary.fingerprints == len(_prints(block(1))) + len(_prints(block(2)))
    with sqlite3.connect(store.index_path(baseline)) as db:
        paths = [row[0] for row in db.execute("SELECT path FROM files ORDER BY id")]
        exts = dict(db.execute("SELECT sha256_uploaded, ext FROM contents"))
    assert paths == ["b.py", "src/a.js", "src/b.js"]
    assert exts[sha(a)] == ".js" and exts[sha(b"x = 1\n")] == ".py"


def test_one_content_under_two_paths_is_indexed_once(tmp_path: Path) -> None:
    code = block(3).encode()
    baseline = _build(tmp_path, make_part({"a/x.js": code, "b/x.js": code}))
    index = _open(tmp_path, baseline)
    [ref] = index.with_hash(sha(code))
    assert (ref.path, ref.other_paths, ref.content) == ("a/x.js", 1, sha(code))
    assert ref.baseline_id == index.id == str(baseline)


def test_candidates_are_the_contents_sharing_the_most_fingerprints(tmp_path: Path) -> None:
    near, far = block(4), block(5)
    half = "\n".join(near.splitlines()[:7]) + "\n" + block(6)
    baseline = _build(
        tmp_path,
        make_part({"near.js": near.encode(), "half.js": half.encode(), "far.js": far.encode()}),
    )
    found = _open(tmp_path, baseline).candidates(_prints(near), 20)
    assert [ref.path for ref, _ in found] == ["near.js", "half.js"]
    assert found[0][1] == len(_prints(near)) > found[1][1] > 0
    assert len(_open(tmp_path, baseline).candidates(_prints(near), 1)) == 1


def test_a_file_is_found_by_the_hash_it_had_on_the_device(tmp_path: Path) -> None:
    sent = b'const key = "HACKPROOF_BLANKED";\n' + block(7).encode()
    on_device = b'const key = "sk-live-1";\n' + block(7).encode()
    blanked = Entry("cfg.js", len(sent), sha(on_device), sha(sent), keys_blanked=1)
    baseline = _build(tmp_path, pack([blanked.__dict__], [(sha(sent), sent)]))
    index = _open(tmp_path, baseline)
    assert [r.path for r in index.with_hash(sha(on_device))] == ["cfg.js"]
    assert index.with_hash(sha(sent)) == []


def test_an_entry_whose_content_never_came_is_counted_not_fatal(tmp_path: Path) -> None:
    lost = entry("lost.js", b"never sent")
    baseline = uuid.uuid4()
    store = _store(tmp_path)
    store.write_part(baseline, 1, make_part({"a.js": b"x = 1;"}, extra_entries=[lost]))
    summary = build_index(tmp_path, KEY, baseline, 1, "a1", LIMIT, S)
    assert (summary.files, summary.contents, summary.missing_content) == (2, 1, 1)
    assert _open(tmp_path, baseline).with_hash(sha(b"never sent")) == []


def test_the_index_records_its_settings(tmp_path: Path) -> None:
    baseline = _build(tmp_path, make_part({"a.js": b"x"}))
    with sqlite3.connect(_store(tmp_path).index_path(baseline)) as db:
        meta = dict(db.execute("SELECT key, value FROM meta"))
    assert meta == {"k": "25", "w": "16", "lexer_version": "1", "hash_version": "1"}


def test_the_old_text_is_read_back_from_its_encrypted_part(tmp_path: Path) -> None:
    first, second = block(8), "caf\xe9 = 1;\n"
    baseline = _build(
        tmp_path,
        make_part({"one.js": first.encode()}),
        make_part({"two.js": second.encode("latin-1")}),
    )
    index = _open(tmp_path, baseline)
    [one] = index.with_hash(sha(first.encode()))
    [two] = index.with_hash(sha(second.encode("latin-1")))
    assert index.text(one) == first
    assert index.text(two) == "caf� = 1;\n"  # not UTF-8: replaced, never fatal
    index.close()


def test_holders_counts_teams_not_devices(tmp_path: Path) -> None:
    shared, own = block(9), block(10)
    store = _store(tmp_path)
    laptop = _build(tmp_path, make_part({"a.js": shared.encode()}))
    desktop = _build(tmp_path, make_part({"b.js": shared.encode()}))
    other = _build(tmp_path, make_part({"c.js": shared.encode(), "d.js": own.encode()}))
    indexes = {
        "team-a": [store.index_path(laptop), store.index_path(desktop)],
        "team-b": [store.index_path(other)],
    }
    wanted = _prints(shared) | _prints(own) | {12345}
    counts = holders(indexes, wanted)
    assert all(counts[h] == 2 for h in _prints(shared))
    assert all(counts[h] == 1 for h in _prints(own) - _prints(shared))
    assert 12345 not in counts


def test_a_half_built_index_is_never_seen(tmp_path: Path) -> None:
    baseline = uuid.uuid4()
    store = _store(tmp_path)
    store.write_part(baseline, 1, make_part({"a.js": block(11).encode()}))
    store.write_part(baseline, 2, make_part({"b.js": b"y"}))
    sealed = store.dir(baseline) / "parts" / "2.bin"
    damaged = bytearray(sealed.read_bytes())
    damaged[20] ^= 1
    sealed.write_bytes(bytes(damaged))

    with pytest.raises(BaselineCorrupt):
        build_index(tmp_path, KEY, baseline, 2, "a1", LIMIT, S)

    assert not store.index_path(baseline).exists()
    assert not list(store.dir(baseline).glob("index.*"))


def test_an_indexed_baseline_gives_the_matches_a_memory_one_gives(tmp_path: Path) -> None:
    a, b, c = block(20), block(21), block(22)
    old = {"lib/a.js": a, "lib/b.js": b + c, "copy/a.js": a}
    baseline = _build(tmp_path, make_part({p: t.encode() for p, t in old.items()}))
    final = [source("x.js", a + block(30) + b), source("y.js", c), source("z.js", a)]

    def report(base: object) -> object:
        return compare(
            final,
            [base],  # type: ignore[list-item]
            starter_runs=set(),
            common_teams=nobody,
            min_teams=3,
            settings=S,
        )

    from_memory = report(MemoryBaseline(str(baseline), old))
    from_index = report(_open(tmp_path, baseline))
    assert from_index == from_memory
