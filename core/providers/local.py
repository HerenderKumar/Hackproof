"""Bare repositories on disk, so fixtures and tests run offline (D-006).

A repository is `LOCAL_REMOTES_DIR/<name>.git` and its URL is `local://<name>`.
"""

import re
from datetime import datetime
from pathlib import Path

from core.clock import Clock
from core.git import has_commit, run_git
from core.git_compare import compare_commits
from core.repo import (
    Comparison,
    InvalidRepoUrl,
    ProviderName,
    RepoInfo,
    RepoNotFound,
    SnapshotFiles,
)
from core.snapshots import archive_commit

SCHEME = "local://"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class LocalGitProvider:
    name: ProviderName = "local"

    def __init__(self, remotes_dir: Path, clock: Clock) -> None:
        self._remotes_dir = remotes_dir
        self._clock = clock

    def _path(self, repo_url: str) -> tuple[str, Path]:
        name = repo_url.strip().removeprefix(SCHEME)
        # A name is one path segment of plain characters, so it cannot leave the directory.
        if not _NAME.match(name):
            raise InvalidRepoUrl(f"use {SCHEME}<name>, where the name has letters, digits, - or _")
        return name, self._remotes_dir / f"{name}.git"

    async def describe(self, repo_url: str) -> RepoInfo:
        name, path = self._path(repo_url)
        if not path.is_dir():
            raise RepoNotFound(f"no local repository named {name}")
        branch = await run_git("symbolic-ref", "--short", "HEAD", git_dir=path)
        # An empty repository has no commits to date it by.
        first_commit = await run_git(
            "log", "--reverse", "--format=%cI", "--all", git_dir=path, check=False
        )
        created_at = (
            datetime.fromisoformat(first_commit.splitlines()[0])
            if first_commit
            else self._clock.now()
        )
        return RepoInfo(
            provider="local",
            id=name,
            url=f"{SCHEME}{name}",
            default_branch=branch,
            created_at=created_at,
            is_public=True,
        )

    async def head(self, repo: RepoInfo) -> str | None:
        name, path = self._path(repo.url)
        if not path.is_dir():
            raise RepoNotFound(f"no local repository named {name}")
        sha = await run_git(
            "rev-parse", "--verify", "--quiet", f"{repo.default_branch}^{{commit}}",
            git_dir=path, check=False,
        )  # fmt: skip
        return sha or None

    async def compare(self, repo: RepoInfo, base: str | None, head: str) -> Comparison:
        name, path = self._path(repo.url)
        if not path.is_dir():
            raise RepoNotFound(f"no local repository named {name}")
        if base is not None and not await has_commit(path, base):
            return Comparison(status="diverged", lines_added=0, files_changed=0, commits=[])
        return await compare_commits(path, base, head)

    async def snapshot(self, repo: RepoInfo, sha: str, archives_dir: Path) -> SnapshotFiles:
        name, path = self._path(repo.url)
        if not path.is_dir():
            raise RepoNotFound(f"no local repository named {name}")
        return await archive_commit(path, sha, archives_dir)
