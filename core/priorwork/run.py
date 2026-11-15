"""A final snapshot against the team's stored baselines, in one picklable call (M9, D-052).

`run_comparison` is what the `prior_work` job hands to its process pool: everything it needs
is in the request, as plain values, so it runs in another process without the database. Other
teams' baselines only decide which code is common (held by `min_teams` teams or more); only the
team's own baselines can match.
"""

import hashlib
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from core.baselines.index import IndexedBaseline, holders
from core.baselines.store import BaselineStore
from core.priorwork.compare import compare, eligible
from core.priorwork.fingerprint import pack_runs, run_hashes, unpack_runs
from core.priorwork.lexer import tokenize
from core.priorwork.model import MatchSettings, Report, SourceFile
from core.snapshots import read_archive


@dataclass(frozen=True)
class BaselineRef:
    baseline_id: uuid.UUID
    team: str
    device_label: str
    late: bool


@dataclass(frozen=True)
class CompareRequest:
    archive: Path
    archive_sha256: str
    extensions: tuple[str, ...]
    max_file_bytes: int
    store_root: Path
    key: bytes
    own: tuple[BaselineRef, ...]  # the team's indexed baselines
    others: tuple[BaselineRef, ...]  # every other indexed baseline of the event
    starter_runs: tuple[bytes, ...]  # each starter's packed runs
    min_teams: int
    settings: MatchSettings


def final_files(request: CompareRequest) -> list[SourceFile]:
    def wanted(path: str, size: int) -> bool:
        return eligible(path, size, request.extensions, request.max_file_bytes)

    return [
        SourceFile(path, hashlib.sha256(data).hexdigest(), data.decode("utf-8", errors="replace"))
        for path, data in read_archive(request.archive, request.archive_sha256, wanted)
    ]


def run_comparison(request: CompareRequest) -> Report:
    store = BaselineStore(request.store_root, request.key)
    own = [
        IndexedBaseline(store, ref.baseline_id, device_label=ref.device_label, late=ref.late)
        for ref in request.own
    ]
    teams: dict[str, list[Path]] = {}
    for ref in (*request.own, *request.others):
        teams.setdefault(ref.team, []).append(store.index_path(ref.baseline_id))

    def common_teams(hashes: Iterable[int]) -> Mapping[int, int]:
        return holders(teams, list(hashes))

    starter: set[int] = set()
    for packed in request.starter_runs:
        starter |= unpack_runs(packed)
    try:
        return compare(
            final_files(request),
            own,
            starter_runs=starter,
            common_teams=common_teams,
            min_teams=request.min_teams,
            settings=request.settings,
        )
    finally:
        for baseline in own:
            baseline.close()


def starter_runs(files: Iterable[tuple[str, bytes]], settings: MatchSettings) -> bytes:
    """Every run of `k` tokens in a starter's files, packed: code a starter holds is never a
    match (A8 step 1). The caller passes only eligible files."""
    runs: set[int] = set()
    for path, data in files:
        text = data.decode("utf-8", errors="replace")
        syms = [t.sym for t in tokenize(text, PurePosixPath(path).suffix)]
        runs.update(run_hashes(syms, settings.k))
    return pack_runs(runs)
