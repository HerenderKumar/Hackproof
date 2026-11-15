"""A "possibly related" list beside the percentage (M9 plan, Stage 5, D-052).

Old code written again (other names, another structure) can keep no stretch of 25 tokens and so
never counts. For a final file less than half matched, the team's old files that share its rarest
names are cut into pieces (from one definition to the next, or 40-line blocks), and an `Embedder`
scores each final piece against them. Pairs at or above a score are listed as `possibly_related`
matches with 0 tokens: for a judge to check by eye. They never change `Totals`.

Baseline text goes only to the `Embedder` it is given: in the worker that is a model loaded in
the same process (worker/embedder.py), never a remote service.
"""

import math
import re
from collections.abc import Collection, Sequence
from pathlib import PurePosixPath
from typing import Protocol

from core.priorwork.compare import excerpt
from core.priorwork.lexer import NAME, family, tokenize
from core.priorwork.model import Baseline, Match, OldRef, Report, SourceFile

STARTS = frozenset(
    {"def", "class", "function", "func", "fn", "public", "private", "protected", "static"}
    | {"async", "export"}
)
PIECE_MAX_LINES = 80
PLAIN_PIECE_LINES = 40  # a file with no definition line
MIN_PIECE_LINES = 3  # lines with code; shorter pieces say too little to compare
OLD_FILES = 5
RARE_NAMES = 64  # the final file's rarest names are the ones looked up
MAX_RELATED = 50
MAX_PIECES = 1500  # distinct texts embedded for one report

_FIRST_WORD = re.compile(r"\s*([A-Za-z_]\w*)")


class Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class NamedBaseline(Baseline, Protocol):
    """A `Baseline` that also knows which names each old content holds."""

    def contents(self) -> int: ...

    def name_counts(self, names: Collection[str]) -> dict[str, int]:
        """Per name, how many contents hold it."""
        ...

    def holding(self, names: Collection[str]) -> dict[str, tuple[str, frozenset[str]]]:
        """Per content holding any of `names`: its extension and the names it holds."""
        ...

    def ref(self, content: str) -> OldRef: ...


def _ext(path: str) -> str:
    return PurePosixPath(path).suffix.lower()


def pieces(text: str, ext: str) -> list[tuple[int, int]]:
    """Line ranges (first and last, from 1) to compare one by one. `ext` is the file's own; the
    start words are the same for every language."""
    del ext
    lines = text.splitlines()
    starts = [n for n, line in enumerate(lines, 1) if _starts(line)]
    if starts:
        ends = [*(s - 1 for s in starts[1:]), len(lines)]
        spans = list(zip(starts, ends, strict=True))
    else:
        spans = [(1, len(lines))] if lines else []
    size = PIECE_MAX_LINES if starts else PLAIN_PIECE_LINES
    out = []
    for first, last in spans:
        for start in range(first, last + 1, size):
            end = min(last, start + size - 1)
            if sum(1 for line in lines[start - 1 : end] if line.strip()) >= MIN_PIECE_LINES:
                out.append((start, end))
    return out


def _starts(line: str) -> bool:
    found = _FIRST_WORD.match(line)
    return found is not None and found.group(1) in STARTS


def _names(text: str, ext: str) -> set[str]:
    return {t.text for t in tokenize(text, ext) if t.sym == NAME}


def pick_old_files(
    final: SourceFile, baselines: Sequence[NamedBaseline], limit: int = OLD_FILES
) -> list[OldRef]:
    """The old files of the same language family scored by the rare names they share with
    `final`: the sum of log((N + 1) / n) over those names, n being how many of the team's N
    contents hold the name (the plan's log(N / n), plus one so a name every content holds still
    counts a little, and a device of one file can be picked). The best `limit`, best first."""
    ext = _ext(final.path)
    names = _names(final.text, ext)
    if not names or not baselines:
        return []
    held: dict[str, int] = {}
    for baseline in baselines:
        for name, count in baseline.name_counts(names).items():
            held[name] = held.get(name, 0) + count
    total = sum(baseline.contents() for baseline in baselines)
    rare = sorted(held, key=lambda name: (held[name], name))[:RARE_NAMES]
    weight = {name: math.log((total + 1) / held[name]) for name in rare}
    scored: list[tuple[float, str, str, NamedBaseline]] = []
    for baseline in baselines:
        for content, (old_ext, shared) in baseline.holding(rare).items():
            if family(old_ext) != family(ext):
                continue
            score = sum(weight[name] for name in shared)
            if score > 0:
                scored.append((score, baseline.id, content, baseline))
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    picked: list[OldRef] = []
    seen: set[str] = set()
    for _, _, content, baseline in scored:
        if content not in seen and len(picked) < limit:
            seen.add(content)
            picked.append(baseline.ref(content))
    return picked


def _cut(text: str, span: tuple[int, int]) -> str:
    first, last = span
    return "\n".join(text.splitlines()[first - 1 : last])


def _unit(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm else [0.0 for _ in vector]


def related(
    report: Report,
    files: Sequence[SourceFile],
    baselines: Sequence[NamedBaseline],
    embedder: Embedder,
    min_score: float,
    *,
    max_pieces: int = MAX_PIECES,
) -> tuple[Match, ...]:
    """The `possibly_related` matches of a report, best first, at most `MAX_RELATED`."""
    shares = {share.path: share for share in report.files}
    covered: dict[str, list[tuple[int, int]]] = {}
    for m in report.matches:
        covered.setdefault(m.final_path, []).append((m.final_start_line, m.final_end_line))
    by_id = {baseline.id: baseline for baseline in baselines}
    texts: dict[str, int] = {}  # each distinct text once, by its place in the list to embed
    work: list[
        tuple[SourceFile, list[tuple[int, int]], list[tuple[OldRef, str, list[tuple[int, int]]]]]
    ] = []
    for f in sorted(files, key=lambda f: f.path):
        share = shares.get(f.path)
        counted = share.total - share.excluded if share else 0
        if share is None or counted <= 0 or 2 * (share.matched + share.late_matched) >= counted:
            continue
        mine = [
            span
            for span in pieces(f.text, _ext(f.path))
            if not any(
                span[0] <= end and start <= span[1] for start, end in covered.get(f.path, [])
            )
        ]
        if not mine:
            continue
        olds = []
        for ref in pick_old_files(f, baselines):
            text = by_id[ref.baseline_id].text(ref)
            spans = pieces(text, _ext(ref.path))
            if spans:
                olds.append((ref, text, spans))
        if not olds:
            continue
        wanted = {_cut(f.text, span) for span in mine}
        wanted |= {_cut(text, span) for _, text, spans in olds for span in spans}
        new = wanted - texts.keys()
        if len(texts) + len(new) > max_pieces:
            continue
        for text in sorted(new):
            texts[text] = len(texts)
        work.append((f, mine, olds))
    if not work:
        return ()
    vectors = [_unit(v) for v in embedder.embed(list(texts))]

    def vector(text: str, span: tuple[int, int]) -> list[float]:
        return vectors[texts[_cut(text, span)]]

    found: list[Match] = []
    for f, mine, olds in work:
        for span in mine:
            here = vector(f.text, span)
            best: tuple[float, OldRef, str, tuple[int, int]] | None = None
            for ref, text, spans in olds:
                for old_span in spans:
                    score = sum(a * b for a, b in zip(here, vector(text, old_span), strict=True))
                    if best is None or score > best[0]:
                        best = (score, ref, text, old_span)
            if best is None or best[0] < min_score:
                continue
            score, ref, text, old_span = best
            baseline = by_id[ref.baseline_id]
            found.append(
                Match(
                    final_path=f.path,
                    final_start_line=span[0],
                    final_end_line=span[1],
                    tokens=0,
                    kind="possibly_related",
                    late=baseline.late,
                    baseline_id=ref.baseline_id,
                    device_label=baseline.device_label,
                    old_path=ref.path,
                    old_start_line=old_span[0],
                    old_end_line=old_span[1],
                    also_in=ref.other_paths,
                    final_excerpt=excerpt(f.text, span[0], span[1], 0),
                    old_excerpt=excerpt(text, old_span[0], old_span[1], 0),
                    score=round(score, 4),
                )
            )
    found.sort(key=lambda m: (-(m.score or 0.0), m.final_path, m.final_start_line))
    return tuple(found[:MAX_RELATED])


def with_related(
    report: Report,
    files: Sequence[SourceFile],
    baselines: Sequence[NamedBaseline],
    embedder: Embedder,
    min_score: float,
) -> Report:
    """The report with its `possibly_related` matches after the others; the totals and the
    per-file shares stay as they were."""
    extra = related(report, files, baselines, embedder, min_score)
    return Report(report.totals, report.files, report.matches + extra)
