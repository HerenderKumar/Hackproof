"""Prior-work signals (section 6.6), as pure functions over `repo_observations` rows.

A signal is context for a judge's question, never a verdict (invariant 5): nothing here feeds a
score, a test result or an ordering of teams. Commit dates are the team's own word, so a signal
built on them says so; `observed_at` times are the platform's.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

SIGNAL_ORDER = (
    "bulk_first_push",
    "commits_before_kickoff",
    "repo_created_before_event",
    "late_registration",
    "history_rewritten",
)
BASELINE_KINDS = ("kickoff", "late_registration")


class Observation(Protocol):
    """The columns of `models.RepoObservation` that signals read."""

    @property
    def kind(self) -> str: ...
    @property
    def observed_at(self) -> datetime: ...
    @property
    def head_sha(self) -> str | None: ...
    @property
    def previous_head_sha(self) -> str | None: ...
    @property
    def descends_from_previous(self) -> bool | None: ...
    @property
    def lines_added(self) -> int | None: ...
    @property
    def lines_total(self) -> int | None: ...
    @property
    def commits(self) -> Any: ...


@dataclass(frozen=True)
class Signal:
    name: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PushEntry:
    observed_at: datetime
    kind: str
    head_sha: str | None
    previous_head_sha: str | None
    descends_from_previous: bool | None
    lines_added: int | None


def signals(
    *,
    kickoff_at: datetime,
    bulk_push_lines: int,
    bulk_push_window_minutes: int,
    repo_created_at: datetime | None,
    observations: Sequence[Observation],
) -> list[Signal]:
    """Every signal the observations support, in `SIGNAL_ORDER`."""
    rows = _ordered(observations)
    polls = [o for o in rows if o.kind == "poll"]
    found: list[Signal] = []

    window_end = kickoff_at + timedelta(minutes=bulk_push_window_minutes)
    bulk = [
        o
        for o in polls
        if kickoff_at <= o.observed_at <= window_end and (o.lines_added or 0) >= bulk_push_lines
    ]
    if bulk:
        first = bulk[0]
        found.append(
            Signal(
                "bulk_first_push",
                {
                    "observed_at": first.observed_at.isoformat(),
                    "head_sha": first.head_sha,
                    "lines_added": first.lines_added,
                    "minutes_after_kickoff": int((first.observed_at - kickoff_at).total_seconds())
                    // 60,
                },
            )
        )

    early: dict[str, str] = {}
    for row in rows:
        for commit in row.commits or []:
            when = _parse(commit.get("author_date")) if isinstance(commit, dict) else None
            if when is not None and when < kickoff_at:
                early.setdefault(str(commit.get("sha")), commit["author_date"])
    if early:
        found.append(
            Signal(
                "commits_before_kickoff",
                {
                    "commits": [{"sha": s, "author_date": d} for s, d in early.items()],
                    "self_reported": True,
                },
            )
        )

    if repo_created_at is not None and repo_created_at < kickoff_at:
        found.append(
            Signal("repo_created_before_event", {"created_at": repo_created_at.isoformat()})
        )

    late = next((o for o in rows if o.kind == "late_registration"), None)
    if late is not None:
        found.append(Signal("late_registration", {"observed_at": late.observed_at.isoformat()}))

    rewrites = [o for o in polls if o.descends_from_previous is False]
    if rewrites:
        found.append(
            Signal(
                "history_rewritten",
                {
                    "rewrites": [
                        {
                            "observed_at": o.observed_at.isoformat(),
                            "head_sha": o.head_sha,
                            "previous_head_sha": o.previous_head_sha,
                        }
                        for o in rewrites
                    ]
                },
            )
        )
    return found


def describe(signal: Signal) -> str:
    """One plain sentence for the brief page. It states what was observed, not what it means."""
    d = signal.detail
    if signal.name == "bulk_first_push":
        return (
            f"{d['lines_added']} lines arrived in one push, seen {d['minutes_after_kickoff']} "
            f"minutes after kickoff (head {_short(d['head_sha'])})."
        )
    if signal.name == "commits_before_kickoff":
        n = len(d["commits"])
        return (
            f"{n} commit{'s' if n != 1 else ''} carr{'y' if n != 1 else 'ies'} an author date "
            "before kickoff. Commit dates are set by whoever commits, so they are self-reported."
        )
    if signal.name == "repo_created_before_event":
        return f"The repository was created before kickoff ({d['created_at'][:10]})."
    if signal.name == "late_registration":
        return "The repository was registered after kickoff, so its state at kickoff was not seen."
    if signal.name == "history_rewritten":
        parts = [
            f"{_short(r['head_sha'])} replaced {_short(r['previous_head_sha'])}"
            for r in d["rewrites"]
        ]
        return (
            "The branch's history was rewritten: "
            + "; ".join(parts)
            + ". Earlier heads stay in the push log."
        )
    return signal.name.replace("_", " ")


def kickoff_lines(observations: Sequence[Observation]) -> int | None:
    """Lines in the repository when the watch first saw it, or None before that."""
    baseline = next((o for o in _ordered(observations) if o.kind in BASELINE_KINDS), None)
    return baseline.lines_total if baseline is not None else None


def push_log(observations: Sequence[Observation]) -> list[PushEntry]:
    """Every head the watch saw, oldest first; a rewrite never removes an earlier entry."""
    return [
        PushEntry(
            observed_at=o.observed_at,
            kind=o.kind,
            head_sha=o.head_sha,
            previous_head_sha=o.previous_head_sha,
            descends_from_previous=o.descends_from_previous,
            lines_added=o.lines_added,
        )
        for o in _ordered(observations)
    ]


def growth(observations: Sequence[Observation]) -> list[tuple[datetime, int]]:
    """Lines over time: the kickoff row's total, then each poll's lines added on top."""
    points: list[tuple[datetime, int]] = []
    total = 0
    for o in _ordered(observations):
        if o.kind in BASELINE_KINDS:
            total = o.lines_total or 0
        else:
            total += o.lines_added or 0
        points.append((o.observed_at, total))
    return points


def _ordered(observations: Sequence[Observation]) -> list[Observation]:
    return sorted(observations, key=lambda o: o.observed_at)


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def _short(sha: object) -> str:
    return str(sha)[:7] if sha else "none"
