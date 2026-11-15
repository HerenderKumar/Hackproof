"""Prior-work signals (section 6.6) from observation rows: pure functions, no database."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from core.signals import Signal, describe, growth, kickoff_lines, push_log, signals

KICKOFF = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)


@dataclass
class Obs:
    kind: str = "poll"
    minutes: int = 0
    head_sha: str | None = "a" * 40
    previous_head_sha: str | None = None
    descends_from_previous: bool | None = True
    lines_added: int | None = 0
    lines_total: int | None = None
    commits: list[dict[str, Any]] = field(default_factory=list)

    @property
    def observed_at(self) -> datetime:
        return KICKOFF + timedelta(minutes=self.minutes)


def kickoff_row(lines: int = 0, **kw: Any) -> Obs:
    return Obs(
        kind="kickoff", descends_from_previous=None, lines_added=None, lines_total=lines, **kw
    )


def names(observations: list[Obs], created: datetime | None = KICKOFF) -> list[str]:
    found = signals(
        kickoff_at=KICKOFF,
        bulk_push_lines=2000,
        bulk_push_window_minutes=60,
        repo_created_at=created,
        observations=observations,
    )
    return [s.name for s in found]


def test_a_large_poll_inside_the_window_is_a_bulk_first_push() -> None:
    found = signals(
        kickoff_at=KICKOFF,
        bulk_push_lines=2000,
        bulk_push_window_minutes=60,
        repo_created_at=None,
        observations=[kickoff_row(), Obs(minutes=5, lines_added=3087)],
    )
    assert found == [
        Signal(
            "bulk_first_push",
            {
                "observed_at": (KICKOFF + timedelta(minutes=5)).isoformat(),
                "head_sha": "a" * 40,
                "lines_added": 3087,
                "minutes_after_kickoff": 5,
            },
        )
    ]


@pytest.mark.parametrize(
    ("minutes", "lines", "expected"),
    [(5, 1999, False), (5, 2000, True), (60, 2500, True), (61, 2500, False)],
)
def test_the_bulk_push_edges(minutes: int, lines: int, expected: bool) -> None:
    rows = [kickoff_row(), Obs(minutes=minutes, lines_added=lines)]
    assert ("bulk_first_push" in names(rows)) is expected


def test_a_kickoff_row_with_many_lines_is_not_a_bulk_push() -> None:
    assert names([kickoff_row(5000)]) == []


def test_a_commit_dated_before_kickoff_is_reported_as_self_reported() -> None:
    commit = {"sha": "b" * 40, "author_date": "2026-11-04T09:00:00+00:00"}
    found = signals(
        kickoff_at=KICKOFF,
        bulk_push_lines=2000,
        bulk_push_window_minutes=60,
        repo_created_at=None,
        observations=[kickoff_row(), Obs(minutes=5, lines_added=10, commits=[commit])],
    )
    assert found == [
        Signal(
            "commits_before_kickoff",
            {
                "commits": [{"sha": "b" * 40, "author_date": commit["author_date"]}],
                "self_reported": True,
            },
        )
    ]


def test_a_commit_dated_at_kickoff_is_not() -> None:
    commit = {"sha": "b" * 40, "author_date": KICKOFF.isoformat()}
    assert names([kickoff_row(commits=[commit])]) == []


def test_an_unreadable_commit_date_is_ignored() -> None:
    commits = [{"sha": "b" * 40, "author_date": "yesterday"}, {"sha": "c" * 40}]
    assert names([kickoff_row(commits=commits)]) == []


def test_a_repository_older_than_the_event_is_reported() -> None:
    assert names([kickoff_row()], created=KICKOFF - timedelta(days=30)) == [
        "repo_created_before_event"
    ]


def test_a_late_registration_row_is_reported() -> None:
    late = Obs(kind="late_registration", minutes=120, descends_from_previous=None, lines_total=40)
    assert names([late]) == ["late_registration"]


def test_every_rewrite_is_listed_with_the_heads_it_replaced() -> None:
    rows = [
        kickoff_row(head_sha=None),
        Obs(minutes=10, head_sha="a" * 40),
        Obs(
            minutes=20, head_sha="b" * 40, previous_head_sha="a" * 40, descends_from_previous=False
        ),
        Obs(minutes=30, head_sha="c" * 40, previous_head_sha="b" * 40),
        Obs(
            minutes=40, head_sha="d" * 40, previous_head_sha="c" * 40, descends_from_previous=False
        ),
    ]
    [found] = signals(
        kickoff_at=KICKOFF,
        bulk_push_lines=2000,
        bulk_push_window_minutes=60,
        repo_created_at=None,
        observations=rows,
    )
    assert found.name == "history_rewritten"
    assert [(r["previous_head_sha"], r["head_sha"]) for r in found.detail["rewrites"]] == [
        ("a" * 40, "b" * 40),
        ("c" * 40, "d" * 40),
    ]
    assert "bbbbbbb replaced aaaaaaa" in describe(found)


def test_a_quiet_history_has_no_signals() -> None:
    rows = [kickoff_row(10), Obs(minutes=30, lines_added=200), Obs(minutes=90, lines_added=400)]
    assert names(rows) == []


def test_signals_come_in_their_fixed_order() -> None:
    late = Obs(kind="late_registration", minutes=1, descends_from_previous=None, lines_total=0)
    early = {"sha": "b" * 40, "author_date": "2026-11-01T00:00:00Z"}
    rows = [
        late,
        Obs(minutes=5, lines_added=9000, commits=[early]),
        Obs(minutes=9, descends_from_previous=False),
    ]
    assert names(rows, created=KICKOFF - timedelta(days=1)) == [
        "bulk_first_push",
        "commits_before_kickoff",
        "repo_created_before_event",
        "late_registration",
        "history_rewritten",
    ]


def test_growth_adds_each_polls_lines_to_the_kickoff_total() -> None:
    rows = [Obs(minutes=30, lines_added=200), kickoff_row(10), Obs(minutes=90, lines_added=5)]
    assert growth(rows) == [
        (KICKOFF, 10),
        (KICKOFF + timedelta(minutes=30), 210),
        (KICKOFF + timedelta(minutes=90), 215),
    ]
    assert kickoff_lines(rows) == 10
    assert kickoff_lines([]) is None


def test_the_push_log_keeps_every_head_in_order() -> None:
    rows = [
        Obs(minutes=20, head_sha="b" * 40, descends_from_previous=False),
        kickoff_row(head_sha=None),
        Obs(minutes=10, head_sha="a" * 40),
    ]
    assert [e.head_sha for e in push_log(rows)] == [None, "a" * 40, "b" * 40]


def test_every_signal_has_a_sentence_that_is_not_a_verdict() -> None:
    late = Obs(kind="late_registration", minutes=1, descends_from_previous=None, lines_total=0)
    early = {"sha": "b" * 40, "author_date": "2026-11-01T00:00:00Z"}
    rows = [
        late,
        Obs(minutes=5, lines_added=9000, commits=[early]),
        Obs(minutes=9, descends_from_previous=False),
    ]
    found = signals(
        kickoff_at=KICKOFF,
        bulk_push_lines=2000,
        bulk_push_window_minutes=60,
        repo_created_at=KICKOFF - timedelta(days=1),
        observations=rows,
    )
    for signal in found:
        sentence = describe(signal).lower()
        assert sentence and signal.name.replace("_", " ") != sentence
        for word in ("cheat", "plagiar", "suspicious", "violat", "fraud"):
            assert word not in sentence
