"""Bare repositories on disk, so fixtures and tests run offline (D-006).

A repository is `LOCAL_REMOTES_DIR/<name>.git` and its URL is `local://<name>`.
"""

import asyncio
import re
from datetime import datetime
from pathlib import Path

from core.clock import Clock
from core.repo import InvalidRepoUrl, ProviderName, RepoInfo, RepoNotFound

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
        branch = await _git(path, "symbolic-ref", "--short", "HEAD")
        # An empty repository has no commits to date it by.
        first_commit = await _git(path, "log", "--reverse", "--format=%cI", "--all", check=False)
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


async def _git(repo: Path, *args: str, check: bool = True) -> str:
    process = await asyncio.create_subprocess_exec(
        "git",
        "--git-dir",
        str(repo),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0 and check:
        raise RuntimeError(f"git {args[0]} failed: {stderr.decode(errors='replace').strip()}")
    return stdout.decode(errors="replace").strip() if process.returncode == 0 else ""
