"""The demo event's data (M12): its five teams, their repositories, the check of what it made."""

import subprocess
from datetime import timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.signals import signals
from scripts.demo_event import (
    KICKOFF,
    TEAMS,
    build_repositories,
    instants,
    move_heads,
    quick_stats_history,
)
from scripts.fixtures import load_fixtures, load_history_fixtures
from tests.history_replay import replay

Sessions = async_sessionmaker[AsyncSession]

h1 = next(h for h in load_history_fixtures() if h.id == "H1")
f10 = next(f for f in load_fixtures() if f.id == "F10")


def _has_main(repo: Path) -> bool:
    done = subprocess.run(
        ["git", "--git-dir", str(repo), "rev-parse", "--verify", "--quiet", "refs/heads/main"],
        capture_output=True,
        text=True,
    )
    return done.returncode == 0


def _heads(dest: Path) -> dict[str, str]:
    heads = {}
    for repo in sorted(dest.glob("*.git")):
        if _has_main(repo):
            done = subprocess.run(
                ["git", "--git-dir", str(repo), "rev-parse", "refs/heads/main"],
                capture_output=True,
                text=True,
                check=True,
            )
            heads[repo.name.removesuffix(".git")] = done.stdout.strip()
    return heads


def test_the_demo_has_the_designs_five_teams() -> None:
    assert [(t.name, t.repo, t.fixture, t.variant, t.submits) for t in TEAMS] == [
        ("Paisa Split", "paisa-split", "F01", "broken", True),
        ("Pack Light", "pack-light", "F05", "good", True),
        ("Degrees", "degrees", "F09", "good", True),
        ("Quick Stats", "quick-stats", "F10", "good", True),
        ("Pantry Pals", "pantry-pals", "F02", "good", False),
    ]
    for team in TEAMS:
        assert "broken" not in f"{team.name} {team.repo}".lower()
        assert "good" not in f"{team.name} {team.repo}".lower()


def test_quick_stats_is_h1s_history_around_f10s_app() -> None:
    made = quick_stats_history(h1, f10.variant("good"))
    assert [(c.name, c.parent, c.authored, c.committed) for c in made.commits] == [
        (c.name, c.parent, c.authored, c.committed) for c in h1.commits
    ]
    assert made.pushes == h1.pushes and made.slug == "quick-stats"
    kickoff, backdated, bulk = made.commits
    assert set(kickoff.files) == {"README.md"}
    assert kickoff.files["README.md"] == (f10.variant("good").path / "README.md").read_bytes()
    assert set(backdated.files) == {"NOTES.md"}
    assert {"server.js", "package.json", "package-lock.json", "src/engine.js"} <= set(bulk.files)
    assert "README.md" not in bulk.files
    engine = bulk.files["src/engine.js"]
    assert engine is not None and engine.count(b"\n") == 3000
    assert bulk.message == "Add the stats service"


def test_the_repositories_start_with_nothing_pushed_and_never_change(tmp_path: Path) -> None:
    first = build_repositories(tmp_path / "a")
    assert first == build_repositories(tmp_path / "b")  # same SHAs on every build
    assert set(first) == {t.repo for t in TEAMS}
    for repo in first:
        assert not _has_main(tmp_path / "a" / f"{repo}.git")
    assert instants(first) == [
        KICKOFF,
        KICKOFF + timedelta(minutes=5),
        KICKOFF + timedelta(minutes=60),
    ]


def test_heads_move_with_the_clock(tmp_path: Path) -> None:
    pushes = build_repositories(tmp_path)
    move_heads(tmp_path, pushes, KICKOFF - timedelta(hours=1))
    assert _heads(tmp_path) == {}
    move_heads(tmp_path, pushes, KICKOFF)
    assert _heads(tmp_path) == {"quick-stats": pushes["quick-stats"][0][1]}
    move_heads(tmp_path, pushes, KICKOFF + timedelta(minutes=60))
    assert _heads(tmp_path) == {repo: log[-1][1] for repo, log in pushes.items()}


async def test_quick_stats_shows_the_bulk_push_and_the_early_commit(
    sessions: Sessions, tmp_path: Path
) -> None:
    event, team, rows = await replay(
        sessions, quick_stats_history(h1, f10.variant("good")), tmp_path
    )
    found = signals(
        kickoff_at=event.kickoff_at,
        bulk_push_lines=event.bulk_push_lines,
        bulk_push_window_minutes=event.bulk_push_window_minutes,
        repo_created_at=team.repo_created_at,
        observations=rows,
    )
    assert [s.name for s in found] == ["bulk_first_push", "commits_before_kickoff"]
    assert found[0].detail["minutes_after_kickoff"] == 5
    assert found[0].detail["lines_added"] >= 3000
