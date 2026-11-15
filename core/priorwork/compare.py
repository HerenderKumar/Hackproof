"""A final submission against the team's device baselines (the M9 plan's A8, D-052).

For each final file, every token starts free. Starter code and code that many teams hold are
excluded first; then a file identical to an old one is matched whole, and otherwise stretches
equal in symbols to stretches of old files are taken longest first, each claiming the tokens
still free. All of that runs over the baselines sealed by kickoff, then over the late ones for
what is still free. Every counted token belongs to exactly one match, so the percentage is the
share of the eligible tokens that some old file already held.

The percentage is context for a judge's questions: nothing here ranks or scores a team.
"""

from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from core.priorwork.fingerprint import run_hashes, winnow
from core.priorwork.lexer import STRING, Token, tokenize
from core.priorwork.model import (
    KEY_MARKER,
    Baseline,
    Excerpt,
    FileShare,
    Match,
    MatchKind,
    MatchSettings,
    OldRef,
    Report,
    SourceFile,
    Totals,
)
from core.snapshots import counts_toward_lines

FREE, STARTER, COMMON, MATCHED, LATE = 0, 1, 2, 3, 4
_MAX_POSITIONS = 64  # old positions looked at per run hash: repeated boilerplate stays cheap


def eligible(path: str, size: int, extensions: Collection[str], max_file_bytes: int) -> bool:
    ext = PurePosixPath(path).suffix.lower()
    wanted = {e.lower() for e in extensions}
    return ext in wanted and size <= max_file_bytes and counts_toward_lines(path)


def excerpt(text: str, start_line: int, end_line: int, context: int) -> Excerpt:
    lines = text.splitlines()
    first = max(1, start_line - context)
    last = min(len(lines), end_line + context)
    return Excerpt(first, "\n".join(lines[first - 1 : last]))


def _head(text: str, lines: int) -> Excerpt:
    return Excerpt(1, "\n".join(text.splitlines()[:lines]))


class _Old:
    """An old content, tokenised once and indexed by run hash for every final file."""

    def __init__(self, ref: OldRef, baseline: Baseline, k: int) -> None:
        self.ref, self.baseline = ref, baseline
        self.text = baseline.text(ref)
        self.tokens = tokenize(self.text, PurePosixPath(ref.path).suffix)
        self.syms = [t.sym for t in self.tokens]
        self.positions: dict[int, list[int]] = defaultdict(list)
        for j, h in enumerate(run_hashes(self.syms, k)):
            if len(self.positions[h]) < _MAX_POSITIONS:
                self.positions[h].append(j)


@dataclass(frozen=True)
class _Region:
    old: _Old
    start: int  # first final token
    end: int  # last final token, inclusive
    shift: int  # old token = final token + shift

    @property
    def length(self) -> int:
        return self.end - self.start + 1


class _File:
    def __init__(self, source: SourceFile, settings: MatchSettings) -> None:
        self.source = source
        self.ext = PurePosixPath(source.path).suffix
        self.tokens = tokenize(source.text, self.ext)
        self.syms = [t.sym for t in self.tokens]
        self.runs = run_hashes(self.syms, settings.k)
        self.prints = winnow(self.runs, settings.w)
        self.state = [FREE] * len(self.tokens)
        self.common: set[int] = set()  # this file's fingerprints that enough teams hold
        self.done = False


def compare(
    files: Sequence[SourceFile],
    baselines: Sequence[Baseline],
    *,
    starter_runs: Collection[int],
    common_teams: Callable[[Collection[int]], Mapping[int, int]],
    min_teams: int,
    settings: MatchSettings,
) -> Report:
    """`files` are the eligible final files; `common_teams` maps a fingerprint to how many
    teams of the event hold it, this team counted."""
    k = settings.k
    ordered = sorted(baselines, key=lambda b: b.id)
    old_cache: dict[tuple[str, str], _Old] = {}
    matches: list[Match] = []
    shares: list[FileShare] = []
    states: Counter[int] = Counter()

    for source in sorted(files, key=lambda f: f.path):
        f = _File(source, settings)
        _exclude(f, starter_runs, common_teams, min_teams, k)
        for late in (False, True):
            group = [b for b in ordered if b.late == late]
            if group and not f.done:
                matches += _pass(f, group, late, settings, old_cache)
        shares.append(
            FileShare(
                path=source.path,
                total=len(f.state),
                excluded=sum(1 for s in f.state if s in (STARTER, COMMON)),
                matched=f.state.count(MATCHED),
                late_matched=f.state.count(LATE),
            )
        )
        states.update(f.state)

    matches.sort(key=lambda m: (m.late, -m.tokens, m.final_path, m.final_start_line))
    return Report(_totals(states, matches), tuple(shares), tuple(matches))


def _exclude(
    f: _File,
    starter_runs: Collection[int],
    common_teams: Callable[[Collection[int]], Mapping[int, int]],
    min_teams: int,
    k: int,
) -> None:
    starter = set(starter_runs)
    for i, h in enumerate(f.runs):
        if h in starter:
            for t in range(i, i + k):
                f.state[t] = STARTER
    counts = common_teams({h for _, h in f.prints}) if f.prints else {}
    for pos, h in f.prints:
        if counts.get(h, 0) >= min_teams:
            _set_free(f, pos, pos + k - 1, COMMON)
    f.common = {h for h, n in counts.items() if n >= min_teams}


def _pass(
    f: _File,
    group: list[Baseline],
    late: bool,
    settings: MatchSettings,
    cache: dict[tuple[str, str], _Old],
) -> list[Match]:
    mark = LATE if late else MATCHED
    k = settings.k
    found = _identical(f, group, late, settings)
    if found is not None:
        return [found]

    hashes = {h for _, h in f.prints}
    if not hashes:
        return []
    pooled: list[tuple[OldRef, int, Baseline]] = []
    for baseline in group:
        for ref, shared in baseline.candidates(hashes, settings.max_candidates):
            pooled.append((ref, shared, baseline))
    pooled.sort(key=lambda c: (-c[1], c[0].baseline_id, c[0].path))
    olds = []
    for ref, _, baseline in pooled[: settings.max_candidates]:
        key = (ref.baseline_id, ref.content)
        if key not in cache:
            cache[key] = _Old(ref, baseline, k)
        olds.append(cache[key])

    stretches = [r for old in olds for r in _regions(f, old, k)]
    # A region whose fingerprints are all common is excluded whole, before any is taken.
    for region in stretches:
        if _all_common(f, region, k):
            _set_free(f, region.start, region.end, COMMON)
    regions = [r for r in stretches if not _all_common(f, r, k)]
    regions.sort(
        key=lambda r: (-r.length, r.old.ref.baseline_id, r.old.ref.path, r.start + r.shift, r.start)
    )

    out: list[Match] = []
    for region in regions:
        claimed = [t for t in range(region.start, region.end + 1) if f.state[t] == FREE]
        if not claimed:
            continue
        for t in claimed:
            f.state[t] = mark
        first, last = claimed[0], claimed[-1]
        out.append(_region_match(f, region, first, last, len(claimed), late, regions, settings))
    return out


def _identical(
    f: _File, group: list[Baseline], late: bool, settings: MatchSettings
) -> Match | None:
    if len(f.tokens) < settings.k:
        return None
    refs = sorted(
        ((ref, b) for b in group for ref in b.with_hash(f.source.sha256)),
        key=lambda pair: (pair[0].baseline_id, pair[0].path),
    )
    if not refs:
        return None
    f.done = True
    if f.prints and all(h in f.common for _, h in f.prints):
        # A whole file enough teams hold is excluded whole, as a region would be: winnowing
        # leaves a few tokens at its ends outside every common run, and those are not a match.
        _set_free(f, 0, len(f.state) - 1, COMMON)
        return None
    mark = LATE if late else MATCHED
    claimed = 0
    for t, state in enumerate(f.state):
        if state == FREE:
            f.state[t] = mark
            claimed += 1
    if claimed == 0:
        return None
    ref, baseline = refs[0]
    old_text = baseline.text(ref)
    lines = max(1, len(f.source.text.splitlines()))
    return Match(
        final_path=f.source.path,
        final_start_line=1,
        final_end_line=lines,
        tokens=claimed,
        kind="identical_file",
        late=late,
        baseline_id=ref.baseline_id,
        device_label=baseline.device_label,
        old_path=ref.path,
        old_start_line=1,
        old_end_line=max(1, len(old_text.splitlines())),
        also_in=sum(1 + r.other_paths for r, _ in refs) - 1,
        final_excerpt=_head(f.source.text, settings.identical_lines),
        old_excerpt=_head(old_text, settings.identical_lines),
    )


def _regions(f: _File, old: _Old, k: int) -> list[_Region]:
    """Stretches of the final file equal in symbols to stretches of the old one."""
    diagonals: dict[int, list[int]] = defaultdict(list)
    for i, h in enumerate(f.runs):
        for j in old.positions.get(h, ()):
            if f.syms[i : i + k] == old.syms[j : j + k]:
                diagonals[j - i].append(i)
    regions = []
    for shift, starts in diagonals.items():
        run_start = prev = starts[0]
        for i in starts[1:]:
            if i != prev + 1:
                regions.append(_Region(old, run_start, prev + k - 1, shift))
                run_start = i
            prev = i
        regions.append(_Region(old, run_start, prev + k - 1, shift))
    return regions


def _set_free(f: _File, start: int, end: int, state: int) -> None:
    """Give `state` to the tokens from `start` to `end` that are still free."""
    for t in range(start, end + 1):
        if f.state[t] == FREE:
            f.state[t] = state


def _all_common(f: _File, region: _Region, k: int) -> bool:
    """Whether the region has fingerprints (runs wholly inside it) and all of them are common."""
    inside = [h for pos, h in f.prints if region.start <= pos <= region.end - k + 1]
    return bool(inside) and all(h in f.common for h in inside)


def _same_text(a: Token, b: Token) -> bool:
    if a.text == b.text:
        return True
    return a.sym == STRING and b.sym == STRING and (KEY_MARKER in a.text or KEY_MARKER in b.text)


def _region_match(
    f: _File,
    region: _Region,
    first: int,
    last: int,
    claimed: int,
    late: bool,
    regions: list[_Region],
    settings: MatchSettings,
) -> Match:
    old = region.old
    kind: MatchKind = (
        "unchanged"
        if all(
            _same_text(f.tokens[t], old.tokens[t + region.shift]) for t in range(first, last + 1)
        )
        else "edited"
    )
    others = {
        (r.old.ref.baseline_id, r.old.ref.content)
        for r in regions
        if r.old is not old and min(last, r.end) - max(first, r.start) + 1 >= settings.k
    }
    final_start, final_end = f.tokens[first].line, f.tokens[last].line
    old_start = old.tokens[first + region.shift].line
    old_end = old.tokens[last + region.shift].line
    return Match(
        final_path=f.source.path,
        final_start_line=final_start,
        final_end_line=final_end,
        tokens=claimed,
        kind=kind,
        late=late,
        baseline_id=old.ref.baseline_id,
        device_label=old.baseline.device_label,
        old_path=old.ref.path,
        old_start_line=old_start,
        old_end_line=old_end,
        also_in=len(others) + old.ref.other_paths,
        final_excerpt=excerpt(f.source.text, final_start, final_end, settings.context_lines),
        old_excerpt=excerpt(old.text, old_start, old_end, settings.context_lines),
    )


def _percent(part: int, whole: int) -> float | None:
    return round(100 * part / whole, 1) if whole else None


def _totals(states: Counter[int], matches: list[Match]) -> Totals:
    total = sum(states.values())
    matched, late_matched = states[MATCHED], states[LATE]
    by_kind: Counter[str] = Counter()
    for m in matches:
        if not m.late:
            by_kind[m.kind] += m.tokens
    eligible_tokens = total - states[STARTER] - states[COMMON]
    return Totals(
        total=total,
        excluded_starter=states[STARTER],
        excluded_common=states[COMMON],
        eligible=eligible_tokens,
        matched=matched,
        identical=by_kind["identical_file"],
        unchanged=by_kind["unchanged"],
        edited=by_kind["edited"],
        late_matched=late_matched,
        percent=_percent(matched, eligible_tokens),
        late_percent=_percent(late_matched, eligible_tokens),
    )
