"""The prior-work comparison's settings, inputs and report (the M9 plan's A8, D-052).

A `Baseline` is one device's scan as the comparison reads it: which of its files have a given
SHA-256, which old contents share the most fingerprints with a final file, and an old content's
text. The worker's is backed by the baseline's SQLite index and its encrypted parts; the tests'
by a dictionary.
"""

from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal, Protocol

from core.hashing import hash_json
from core.priorwork.fingerprint import HASH_VERSION
from core.priorwork.lexer import LEXER_VERSION

KEY_MARKER = "HACKPROOF_BLANKED"
MatchKind = Literal["identical_file", "unchanged", "edited", "possibly_related"]


@dataclass(frozen=True)
class MatchSettings:
    k: int = 25
    w: int = 16
    max_candidates: int = 20
    context_lines: int = 5
    identical_lines: int = 200
    lexer_version: str = LEXER_VERSION
    hash_version: str = HASH_VERSION

    def stored(self) -> dict[str, int | str]:
        """What is kept with an index or a report, so a later reader knows how it was made."""
        return {
            "k": self.k,
            "w": self.w,
            "lexer_version": self.lexer_version,
            "hash_version": self.hash_version,
        }


@dataclass(frozen=True)
class SourceFile:
    path: str
    sha256: str  # of the file's bytes
    text: str


@dataclass(frozen=True)
class OldRef:
    """One old content of a baseline, by one of its paths."""

    baseline_id: str
    content: str
    path: str
    other_paths: int  # other paths in the same baseline holding the same content


class Baseline(Protocol):
    id: str
    device_label: str
    late: bool

    def with_hash(self, sha256_device: str) -> list[OldRef]: ...

    def candidates(self, hashes: Collection[int], limit: int) -> list[tuple[OldRef, int]]: ...

    def text(self, ref: OldRef) -> str: ...


@dataclass(frozen=True)
class Excerpt:
    first_line: int
    text: str


@dataclass(frozen=True)
class Match:
    final_path: str
    final_start_line: int
    final_end_line: int
    tokens: int
    kind: MatchKind
    late: bool
    baseline_id: str
    device_label: str
    old_path: str
    old_start_line: int
    old_end_line: int
    also_in: int
    final_excerpt: Excerpt
    old_excerpt: Excerpt
    score: float | None = None


@dataclass(frozen=True)
class FileShare:
    path: str
    total: int
    excluded: int
    matched: int
    late_matched: int


@dataclass(frozen=True)
class Totals:
    total: int
    excluded_starter: int
    excluded_common: int
    eligible: int
    matched: int
    identical: int
    unchanged: int
    edited: int
    late_matched: int
    percent: float | None
    late_percent: float | None


@dataclass(frozen=True)
class Report:
    totals: Totals
    files: tuple[FileShare, ...]
    matches: tuple[Match, ...]


def match_key(match: Match) -> str:
    """Names a match across reruns of a report: an explanation is stored against it."""
    return hash_json(
        [
            match.final_path,
            match.final_start_line,
            match.final_end_line,
            match.baseline_id,
            match.old_path,
            match.old_start_line,
            match.old_end_line,
        ]
    )[:32]
