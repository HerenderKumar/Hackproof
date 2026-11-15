"""The repository boundary (section 13.1, D-006).

All access to a team's repository goes through `RepoProvider`. `describe` serves
registration, `head` and `compare` the repo watch (M9), `snapshot` the runs.
"""

from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

ProviderName = Literal["github", "local"]


class RepoInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: ProviderName
    id: str  # GitHub's numeric ID, which survives renames; the repo's name for local repos
    url: str  # the canonical form, stored on the team
    default_branch: str
    created_at: datetime | None
    is_public: bool


class RepoError(Exception):
    """The repository cannot be used, and asking again will not help."""


class InvalidRepoUrl(RepoError):
    pass


class RepoNotFound(RepoError):
    pass


class InvalidCommit(RepoError):
    """Not a full commit SHA. Branch names and short SHAs move; a snapshot pins one commit."""


class CommitNotFound(RepoError):
    pass


class SnapshotFiles(BaseModel):
    """A commit stored as `ARTIFACT_DIR/archives/<sha256>.tar.gz` (section 12.1)."""

    model_config = ConfigDict(frozen=True)

    commit_sha: str
    archive_path: Path
    archive_sha256: str
    # Self-reported, newest first: [{"sha", "author_date", "committer_date", "message"}]
    commit_history: list[dict[str, str]]
    lines_total: int


class Comparison(BaseModel):
    """How a head relates to an earlier one (section 13.2)."""

    model_config = ConfigDict(frozen=True)

    status: Literal["ahead", "behind", "diverged", "identical"]
    lines_added: int  # counted as `lines_total` is: no lockfiles, dependency folders or binaries
    files_changed: int  # every path, counted or not
    # Commits in the head and not in the base, newest first, capped like `commit_history`.
    commits: list[dict[str, str]]


class RepoProvider(Protocol):
    name: ProviderName

    async def describe(self, repo_url: str) -> RepoInfo:
        """Look a repository up.

        Raises `InvalidRepoUrl`, `RepoNotFound`, or `InfraError` when the provider cannot
        be reached or is rate-limiting us.
        """
        ...

    async def head(self, repo: RepoInfo) -> str | None:
        """The SHA of the default branch's last commit, or None when the branch has none yet.

        Raises `InfraError` when the provider cannot be reached.
        """
        ...

    async def compare(self, repo: RepoInfo, base: str | None, head: str) -> Comparison:
        """Compare `head` with `base`; `base=None` means the repository had no commit yet.

        A base the provider no longer has gives `diverged` with zero counts. Raises
        `RepoNotFound` or `InfraError`.
        """
        ...

    async def snapshot(self, repo: RepoInfo, sha: str, archives_dir: Path) -> SnapshotFiles:
        """Archive one commit into `archives_dir`.

        Raises `InvalidCommit`, `CommitNotFound`, `RepoNotFound`, `UnsafeArchive` when the
        commit has a path or link leading out of the project, or `InfraError`.
        """
        ...
