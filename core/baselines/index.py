"""One SQLite index per baseline: its files, contents and fingerprints (M9, the plan's A4).

The index holds hashes and paths, never code: an old content's text is read back from its
encrypted part only when a comparison shows it. `build_index` takes plain arguments so it can
run in the worker's process pool; it writes `index.<attempt>.partial` and renames it, so a
reader finds a whole index or none.
"""

import os
import sqlite3
import time
import uuid
from collections import OrderedDict
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from core.baselines.parts import MAX_UNPACKED_BYTES, Entry, check_part
from core.baselines.store import BaselineStore
from core.priorwork.fingerprint import run_hashes, winnow
from core.priorwork.lexer import tokenize
from core.priorwork.model import MatchSettings, OldRef

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE contents (id INTEGER PRIMARY KEY, sha256_uploaded TEXT NOT NULL UNIQUE,
                       part INTEGER NOT NULL, ext TEXT NOT NULL, tokens INTEGER NOT NULL);
CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT NOT NULL, size INTEGER NOT NULL,
                    sha256_device TEXT NOT NULL, content_id INTEGER, keys_blanked INTEGER NOT NULL);
CREATE INDEX files_by_hash ON files (sha256_device);
CREATE INDEX files_by_content ON files (content_id);
CREATE TABLE fingerprints (hash INTEGER NOT NULL, content_id INTEGER NOT NULL,
                           PRIMARY KEY (hash, content_id)) WITHOUT ROWID;
"""

_CHUNK = 500  # bound parameters per lookup, well under SQLite's limit
_PARTS_KEPT = 4  # decrypted parts an IndexedBaseline keeps in memory


@dataclass(frozen=True)
class IndexSummary:
    files: int
    contents: int
    tokens: int
    fingerprints: int
    missing_content: int
    seconds: float


def _chunks(values: Collection[int]) -> Iterator[list[int]]:
    items = list(values)
    for start in range(0, len(items), _CHUNK):
        yield items[start : start + _CHUNK]


def build_index(
    root: Path,
    key: bytes,
    baseline_id: uuid.UUID,
    parts: int,
    attempt: str,
    max_file_bytes: int,
    settings: MatchSettings,
) -> IndexSummary:
    """Read every part in order, check it again, and write the baseline's index."""
    started = time.monotonic()
    store = BaselineStore(root, key)
    final = store.index_path(baseline_id)
    partial = final.with_name(f"index.{attempt}.partial")
    partial.unlink(missing_ok=True)
    try:
        with sqlite3.connect(partial) as db:
            summary = _fill(db, store, baseline_id, parts, max_file_bytes, settings)
        db.close()
        os.replace(partial, final)
    finally:
        partial.unlink(missing_ok=True)
    return IndexSummary(**summary, seconds=round(time.monotonic() - started, 3))


def _fill(
    db: sqlite3.Connection,
    store: BaselineStore,
    baseline_id: uuid.UUID,
    parts: int,
    max_file_bytes: int,
    settings: MatchSettings,
) -> dict[str, int]:
    db.executescript(SCHEMA)
    db.executemany(
        "INSERT INTO meta VALUES (?, ?)", [(k, str(v)) for k, v in settings.stored().items()]
    )
    entries: list[Entry] = []
    first_ext: dict[str, str] = {}
    content_ids: dict[str, int] = {}
    tokens = 0
    for n in range(1, parts + 1):
        part = check_part(store.read_part(baseline_id, n), max_file_bytes)
        for e in part.entries:
            entries.append(e)
            first_ext.setdefault(e.sha256_uploaded, PurePosixPath(e.path).suffix.lower())
        for digest, data in sorted(part.contents.items()):
            if digest in content_ids:
                continue
            ext = first_ext[digest]
            syms = [t.sym for t in tokenize(data.decode("utf-8", errors="replace"), ext)]
            tokens += len(syms)
            cursor = db.execute(
                "INSERT INTO contents (sha256_uploaded, part, ext, tokens) VALUES (?, ?, ?, ?)",
                (digest, n, ext, len(syms)),
            )
            content_id = cursor.lastrowid
            assert content_id is not None
            content_ids[digest] = content_id
            prints = {h for _, h in winnow(run_hashes(syms, settings.k), settings.w)}
            db.executemany(
                "INSERT INTO fingerprints (hash, content_id) VALUES (?, ?)",
                [(h, content_id) for h in sorted(prints)],
            )
    db.executemany(
        "INSERT INTO files (path, size, sha256_device, content_id, keys_blanked) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (e.path, e.size, e.sha256_device, content_ids.get(e.sha256_uploaded), e.keys_blanked)
            for e in entries
        ],
    )
    fingerprints = db.execute("SELECT count(*) FROM fingerprints").fetchone()[0]
    return {
        "files": len(entries),
        "contents": len(content_ids),
        "tokens": tokens,
        "fingerprints": int(fingerprints),
        "missing_content": sum(1 for e in entries if e.sha256_uploaded not in content_ids),
    }


def _read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


class IndexedBaseline:
    """A `Baseline` (core.priorwork.model) over one stored index and its encrypted parts."""

    def __init__(
        self, store: BaselineStore, baseline_id: uuid.UUID, *, device_label: str, late: bool
    ) -> None:
        self.id, self.device_label, self.late = str(baseline_id), device_label, late
        self._store, self._baseline_id = store, baseline_id
        self._db = _read_only(store.index_path(baseline_id))
        self._parts: OrderedDict[int, dict[str, bytes]] = OrderedDict()

    def close(self) -> None:
        self._db.close()

    def _ref(self, content_id: int) -> OldRef:
        (digest,) = self._db.execute(
            "SELECT sha256_uploaded FROM contents WHERE id = ?", (content_id,)
        ).fetchone()
        paths = [
            row[0]
            for row in self._db.execute(
                "SELECT path FROM files WHERE content_id = ? ORDER BY id", (content_id,)
            )
        ]
        return OldRef(self.id, digest, paths[0], len(paths) - 1)

    def with_hash(self, sha256_device: str) -> list[OldRef]:
        rows = self._db.execute(
            "SELECT DISTINCT content_id FROM files "
            "WHERE sha256_device = ? AND content_id IS NOT NULL ORDER BY content_id",
            (sha256_device,),
        )
        return [self._ref(content_id) for (content_id,) in rows.fetchall()]

    def candidates(self, hashes: Collection[int], limit: int) -> list[tuple[OldRef, int]]:
        shared: dict[int, int] = {}
        for chunk in _chunks(hashes):
            marks = ",".join("?" * len(chunk))
            rows = self._db.execute(
                f"SELECT content_id, count(*) FROM fingerprints WHERE hash IN ({marks}) "
                "GROUP BY content_id",
                chunk,
            )
            for content_id, count in rows:
                shared[content_id] = shared.get(content_id, 0) + count
        found = [(self._ref(content_id), count) for content_id, count in shared.items()]
        found.sort(key=lambda pair: (-pair[1], pair[0].path))
        return found[:limit]

    def text(self, ref: OldRef) -> str:
        (n,) = self._db.execute(
            "SELECT part FROM contents WHERE sha256_uploaded = ?", (ref.content,)
        ).fetchone()
        if n in self._parts:
            self._parts.move_to_end(n)
        else:
            # Checked when it was uploaded and again when indexed; read here without a file limit.
            part = check_part(self._store.read_part(self._baseline_id, n), MAX_UNPACKED_BYTES)
            self._parts[n] = part.contents
            while len(self._parts) > _PARTS_KEPT:
                self._parts.popitem(last=False)
        return self._parts[n][ref.content].decode("utf-8", errors="replace")


def holders(indexes: Mapping[str, Sequence[Path]], hashes: Collection[int]) -> dict[int, int]:
    """Per fingerprint, how many teams hold it. `indexes` maps a team to its index files."""
    counts: dict[int, int] = {}
    for paths in indexes.values():
        held: set[int] = set()
        for path in paths:
            db = _read_only(path)
            try:
                for chunk in _chunks(hashes):
                    marks = ",".join("?" * len(chunk))
                    rows = db.execute(
                        f"SELECT DISTINCT hash FROM fingerprints WHERE hash IN ({marks})", chunk
                    )
                    held.update(h for (h,) in rows)
            finally:
                db.close()
        for h in held:
            counts[h] = counts.get(h, 0) + 1
    return counts
