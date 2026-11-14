"""Fixtures as local bare repositories: app variants and the scripted histories H1 and H2."""

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.providers.local import LocalGitProvider
from scripts import make_history_fixtures
from scripts.fixtures import load_fixtures, load_history_fixtures
from scripts.git_fixtures import PUSH_LOG, build_app_remote, build_history_remote
from tests.fakes import FakeClock

KICKOFF = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
HISTORY = {h.id: h for h in load_history_fixtures()}
F01_GOOD = next(f for f in load_fixtures() if f.id == "F01").variant("good")


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "--git-dir", str(repo), *args], check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def pushes(repo: Path) -> list[dict[str, str]]:
    log: list[dict[str, str]] = json.loads((repo / PUSH_LOG).read_text())
    return log


def minutes(after_kickoff: float) -> str:
    return (KICKOFF + timedelta(minutes=after_kickoff)).isoformat()


# --- app variants ------------------------------------------------------------------------------


def test_an_app_variant_becomes_a_one_commit_repository(tmp_path: Path) -> None:
    repo = build_app_remote(F01_GOOD, tmp_path, KICKOFF)

    assert repo == tmp_path / "F01-node-splitter-good.git"
    assert git(repo, "rev-list", "--count", "main") == "1"
    files = git(repo, "ls-tree", "-r", "--name-only", "main").splitlines()
    assert {"package.json", "server.js", "README.md"} <= set(files)
    assert not any("node_modules" in f for f in files)
    assert git(repo, "show", "main:server.js") == (F01_GOOD.path / "server.js").read_text().strip()


def test_an_app_is_pushed_after_kickoff_so_it_raises_no_prior_work_signal(tmp_path: Path) -> None:
    repo = build_app_remote(F01_GOOD, tmp_path, KICKOFF)

    head = git(repo, "rev-parse", "main")
    [push] = pushes(repo)
    assert push == {"at": minutes(60), "head": head}
    author, committer = git(repo, "log", "-1", "--format=%aI|%cI", "main").split("|")
    assert datetime.fromisoformat(author) == datetime.fromisoformat(committer)
    assert datetime.fromisoformat(author) > KICKOFF


def test_building_a_repository_twice_gives_the_same_commit(tmp_path: Path) -> None:
    first = build_app_remote(F01_GOOD, tmp_path / "a", KICKOFF)
    second = build_app_remote(F01_GOOD, tmp_path / "b", KICKOFF)
    again = build_app_remote(F01_GOOD, tmp_path / "a", KICKOFF)  # rebuilt in place

    assert git(first, "rev-parse", "main") == git(second, "rev-parse", "main")
    assert again == first and git(again, "rev-list", "--count", "--all") == "1"


async def test_a_built_repository_is_one_the_local_provider_can_describe(tmp_path: Path) -> None:
    build_app_remote(F01_GOOD, tmp_path, KICKOFF)

    info = await LocalGitProvider(tmp_path, FakeClock(KICKOFF)).describe(
        "local://F01-node-splitter-good"
    )

    assert (info.id, info.default_branch) == ("F01-node-splitter-good", "main")
    assert info.created_at is not None and info.created_at >= KICKOFF


# --- H1: a bulk first push, with one backdated commit ------------------------------------------


@pytest.fixture
def h1(tmp_path: Path) -> Path:
    return build_history_remote(HISTORY["H1"], tmp_path, KICKOFF)


def test_h1_shows_a_readme_at_kickoff_then_a_large_push_five_minutes_later(h1: Path) -> None:
    log = pushes(h1)

    assert [p["at"] for p in log] == [minutes(0), minutes(5)]
    assert git(h1, "ls-tree", "-r", "--name-only", log[0]["head"]) == "README.md"
    assert git(h1, "rev-parse", "main") == log[1]["head"]
    assert git(h1, "merge-base", "--is-ancestor", log[0]["head"], log[1]["head"]) == ""


def test_h1_adds_three_thousand_lines_in_one_commit(h1: Path) -> None:
    first, second = (p["head"] for p in pushes(h1))

    per_commit = []
    for sha in git(h1, "rev-list", f"{first}..{second}").splitlines():
        stat = git(h1, "show", "--numstat", "--format=", sha).splitlines()
        per_commit.append(sum(int(line.split()[0]) for line in stat))

    assert 3000 in per_commit
    assert sum(per_commit) >= 3000


def test_h1_has_exactly_one_commit_dated_ten_days_before_kickoff(h1: Path) -> None:
    dates = [
        datetime.fromisoformat(line) for line in git(h1, "log", "--format=%aI", "main").splitlines()
    ]

    before = [d for d in dates if d < KICKOFF]
    assert before == [KICKOFF - timedelta(days=10)]


# --- H2: history rewritten after three observed heads ------------------------------------------


@pytest.fixture
def h2(tmp_path: Path) -> Path:
    return build_history_remote(HISTORY["H2"], tmp_path, KICKOFF)


def test_h2_shows_four_heads_in_order(h2: Path) -> None:
    log = pushes(h2)

    assert len(log) == 4 and len({p["head"] for p in log}) == 4
    assert [p["at"] for p in log] == sorted(p["at"] for p in log)
    assert git(h2, "rev-parse", "main") == log[-1]["head"]


def test_h2_grows_normally_and_then_is_rewritten(h2: Path) -> None:
    a, b, c, d = (p["head"] for p in pushes(h2))

    def descends(new: str, old: str) -> bool:
        done = subprocess.run(
            ["git", "--git-dir", str(h2), "merge-base", "--is-ancestor", old, new], check=False
        )
        return done.returncode == 0

    assert descends(b, a) and descends(c, b)
    assert descends(d, a)
    assert not descends(d, c) and not descends(d, b)


def test_h2_keeps_the_rewritten_commits_so_earlier_heads_stay_inspectable(h2: Path) -> None:
    for push in pushes(h2):
        assert git(h2, "cat-file", "-t", push["head"]) == "commit"
    # Nothing is left for `git gc` to prune: every pushed head is held by a ref.
    held = git(h2, "for-each-ref", "--format=%(objectname)").splitlines()
    assert {p["head"] for p in pushes(h2)} <= set(held)


def test_history_repositories_are_reproducible(tmp_path: Path) -> None:
    first = build_history_remote(HISTORY["H2"], tmp_path / "a", KICKOFF)
    second = build_history_remote(HISTORY["H2"], tmp_path / "b", KICKOFF)
    assert pushes(first) == pushes(second)


# --- the command that builds them all ----------------------------------------------------------


def test_the_build_command_writes_every_fixture_repository(tmp_path: Path) -> None:
    argv = ["--dest", str(tmp_path), "--kickoff", KICKOFF.isoformat()]
    assert make_history_fixtures.main(argv) == 0

    built = sorted(p.name for p in tmp_path.glob("*.git"))
    assert len(built) == 21
    assert {"F09-flask-injection-good.git", "H1-bulk-push.git", "H2-force-push.git"} <= set(built)


def test_repositories_use_formats_the_workers_older_git_can_read(tmp_path: Path) -> None:
    repo = build_app_remote(F01_GOOD, tmp_path, KICKOFF)

    assert git(repo, "rev-parse", "--show-object-format") == "sha1"
    assert git(repo, "rev-parse", "--show-ref-format") == "files"
