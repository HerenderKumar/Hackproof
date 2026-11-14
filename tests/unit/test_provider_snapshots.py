"""`RepoProvider.snapshot` for both providers (sections 12.1 and 13.1).

GitHub is exercised by cloning over `file://` from a bare repository laid out like GitHub
(`<base>/<owner>/<name>.git`), so the clone, fetch and archive steps all run for real.
"""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from core.providers.github import GitHubProvider
from core.providers.local import LocalGitProvider
from core.repo import CommitNotFound, RepoInfo, RepoNotFound
from scripts.fixtures import load_history_fixtures
from scripts.git_fixtures import PUSH_LOG, build_history_remote
from tests.fakes import FakeClock

KICKOFF = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
H2 = next(h for h in load_history_fixtures() if h.id == "H2")


def heads(repo: Path) -> list[str]:
    return [push["head"] for push in json.loads((repo / PUSH_LOG).read_text())]


def info(provider: str, url: str, repo_id: str) -> RepoInfo:
    return RepoInfo(
        provider=provider,  # type: ignore[arg-type]
        id=repo_id,
        url=url,
        default_branch="main",
        created_at=KICKOFF,
        is_public=True,
    )


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """H2 as GitHub would hold it: `acme/H2-force-push`, every pushed head still fetchable."""
    repo = build_history_remote(H2, tmp_path / "origin" / "acme", KICKOFF)
    # GitHub serves any commit it still has by SHA, including ones no branch points at.
    subprocess.run(
        ["git", "--git-dir", str(repo), "config", "uploadpack.allowAnySHA1InWant", "true"],
        check=True,
    )
    return repo


def github(tmp_path: Path) -> GitHubProvider:
    return GitHubProvider(
        "gh-token", httpx.AsyncClient(), clone_base=f"file://{tmp_path / 'origin'}"
    )


GITHUB_H2 = info("github", "https://github.com/acme/H2-force-push", "4242")
LOCAL_H2 = info("local", "local://H2-force-push", "H2-force-push")


# --- local ---------------------------------------------------------------------------------------


async def test_local_snapshots_a_commit_from_its_bare_repository(
    origin: Path, tmp_path: Path
) -> None:
    provider = LocalGitProvider(origin.parent, FakeClock(KICKOFF))
    head = heads(origin)[-1]

    files = await provider.snapshot(LOCAL_H2, head, tmp_path / "archives")

    assert files.commit_sha == head
    assert files.archive_path.parent == tmp_path / "archives"
    assert [c["message"] for c in files.commit_history] == [
        "Week view and shopping list",
        "Start the meal planner",
    ]


async def test_local_reports_a_repository_that_is_gone(tmp_path: Path) -> None:
    provider = LocalGitProvider(tmp_path, FakeClock(KICKOFF))

    with pytest.raises(RepoNotFound):
        await provider.snapshot(LOCAL_H2, "a" * 40, tmp_path / "archives")


# --- GitHub --------------------------------------------------------------------------------------


async def test_github_snapshots_the_same_bytes_as_the_repository_itself(
    origin: Path, tmp_path: Path
) -> None:
    head = heads(origin)[-1]
    local = LocalGitProvider(origin.parent, FakeClock(KICKOFF))

    cloned = await github(tmp_path).snapshot(GITHUB_H2, head, tmp_path / "a")
    direct = await local.snapshot(LOCAL_H2, head, tmp_path / "b")

    assert cloned.archive_sha256 == direct.archive_sha256
    assert cloned.commit_history == direct.commit_history
    assert cloned.lines_total == direct.lines_total


async def test_github_fetches_a_commit_no_branch_points_at_any_more(
    origin: Path, tmp_path: Path
) -> None:
    rewritten_away = heads(origin)[1]  # head B, gone from main after the force-push

    files = await github(tmp_path).snapshot(GITHUB_H2, rewritten_away, tmp_path / "archives")

    assert files.commit_sha == rewritten_away
    assert files.commit_history[0]["message"] == "List the week's meals"


async def test_github_reports_a_commit_the_repository_does_not_have(
    origin: Path, tmp_path: Path
) -> None:
    with pytest.raises(CommitNotFound):
        await github(tmp_path).snapshot(GITHUB_H2, "0" * 40, tmp_path / "archives")


async def test_github_reports_a_repository_that_cannot_be_cloned(tmp_path: Path) -> None:
    gone = info("github", "https://github.com/acme/vanished", "4243")

    with pytest.raises(RepoNotFound):
        await github(tmp_path).snapshot(gone, "a" * 40, tmp_path / "archives")


async def test_github_leaves_no_clone_behind(origin: Path, tmp_path: Path) -> None:
    clones = tmp_path / "clones"
    provider = GitHubProvider(
        "gh-token",
        httpx.AsyncClient(),
        clone_base=f"file://{tmp_path / 'origin'}",
        work_dir=clones,
    )

    await provider.snapshot(GITHUB_H2, heads(origin)[-1], tmp_path / "archives")

    assert list(clones.iterdir()) == []
