"""Public GitHub repositories through the REST API (D-005). No GitHub App, no webhooks."""

import re
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from core.errors import InfraError
from core.git import GitFailed, run_git
from core.git_compare import compare_commits
from core.logging import get_logger
from core.repo import (
    CommitNotFound,
    Comparison,
    InvalidRepoUrl,
    ProviderName,
    RepoInfo,
    RepoNotFound,
    SnapshotFiles,
)
from core.snapshots import HISTORY_LIMIT, archive_commit, check_commit_sha, counts_toward_lines

API = "https://api.github.com"
CLONE_TIMEOUT_S = 180.0
# GitHub's compare answer lists at most 300 files; at that many, count from a clone instead.
COMPARE_FILE_LIMIT = 300
log = get_logger()
# What git prints when the repository is not there for us: gone, private, or renamed away.
_NO_REPO = ("not found", "does not appear to be a git repository", "could not read username")
# What a server prints when it does not have the commit asked for.
_NO_COMMIT = ("not our ref", "couldn't find remote ref", "no such remote ref")
_HOSTS = {"github.com", "www.github.com"}
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def parse_repo_url(repo_url: str) -> tuple[str, str]:
    """The owner and name in a repository page URL such as `https://github.com/owner/repo`.

    Only the host and these two path segments are ever used, and requests always go to
    api.github.com, so a submitted URL cannot point the server anywhere else.
    """
    parts = urlsplit(repo_url.strip())
    segments = [s for s in parts.path.split("/") if s]
    if parts.scheme not in ("http", "https") or parts.hostname not in _HOSTS or len(segments) != 2:
        raise InvalidRepoUrl("use the repository's page URL, like https://github.com/owner/repo")
    owner, name = segments[0], segments[1].removesuffix(".git")
    if not _NAME.match(owner) or not _NAME.match(name) or ".." in name:
        raise InvalidRepoUrl("the owner or repository name has characters GitHub does not allow")
    return owner, name


class GitHubProvider:
    name: ProviderName = "github"

    def __init__(
        self,
        token: str,
        client: httpx.AsyncClient,
        *,
        clone_base: str = "https://github.com",
        work_dir: Path | None = None,
    ) -> None:
        self._client = client
        self._clone_base = clone_base.rstrip("/")
        self._work_dir = work_dir
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "hackproof",
        }
        if token:
            self._headers["Authorization"] = f"Bearer {token}"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def describe(self, repo_url: str) -> RepoInfo:
        owner, name = parse_repo_url(repo_url)
        data = await self._get(f"/repos/{owner}/{name}")
        return RepoInfo(
            provider="github",
            id=str(data["id"]),
            url=f"https://github.com/{data['full_name']}",
            default_branch=data["default_branch"],
            created_at=data["created_at"],
            is_public=not data["private"],
        )

    async def head(self, repo: RepoInfo) -> str | None:
        """The default branch's commit. GitHub answers 404 for a branch with no commit yet."""
        owner, name = parse_repo_url(repo.url)
        branch = quote(repo.default_branch, safe="")
        try:
            data = await self._get(f"/repos/{owner}/{name}/branches/{branch}")
        except RepoNotFound:
            return None
        commit = data.get("commit")
        sha = commit.get("sha") if isinstance(commit, dict) else None
        if not isinstance(sha, str):
            raise InfraError("GitHub answered a branch without a commit SHA")
        return sha

    async def compare(self, repo: RepoInfo, base: str | None, head: str) -> Comparison:
        """GitHub's compare answer when it is complete; otherwise git on a partial clone."""
        check_commit_sha(head)
        owner, name = parse_repo_url(repo.url)
        if base is not None:
            check_commit_sha(base)
            try:
                data = await self._get(f"/repos/{owner}/{name}/compare/{base}...{head}")
            except RepoNotFound:
                data = None  # the base is gone, or the repository is: the clone tells which
            if data is not None:
                files = data.get("files")
                if isinstance(files, list) and len(files) < COMPARE_FILE_LIMIT:
                    return _comparison(data, files)
        async with self._clone(owner, name) as git_dir:
            await self._fetch(git_dir, head)
            if base is not None:
                try:
                    await self._fetch(git_dir, base)
                except CommitNotFound:
                    log.warning("compare_base_gone", repo=repo.url, base=base, head=head)
                    return Comparison(status="diverged", lines_added=0, files_changed=0, commits=[])
            return await compare_commits(git_dir, base, head)

    async def snapshot(self, repo: RepoInfo, sha: str, archives_dir: Path) -> SnapshotFiles:
        """Clone without file contents, fetch the commit by SHA, check it out, archive it.

        The clone is public and anonymous: the token is never put on a command line.
        """
        check_commit_sha(sha)
        owner, name = parse_repo_url(repo.url)
        async with self._clone(owner, name) as git_dir:
            await self._fetch(git_dir, sha)
            # Checking the commit out fetches its files in one batch; archiving a partial
            # clone directly would fetch them one request at a time.
            await run_git(
                "checkout", "--quiet", "--detach", sha, cwd=git_dir.parent,
                timeout_s=CLONE_TIMEOUT_S,
            )  # fmt: skip
            return await archive_commit(git_dir, sha, archives_dir)

    @asynccontextmanager
    async def _clone(self, owner: str, name: str) -> AsyncIterator[Path]:
        """A clone without file contents in a scratch folder removed afterwards; its git dir."""
        url = f"{self._clone_base}/{owner}/{name}.git"
        if self._work_dir is not None:
            self._work_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="hp-clone-", dir=self._work_dir) as scratch:
            work = Path(scratch) / "repo"
            try:
                await run_git(
                    "clone", "--quiet", "--filter=blob:none", "--no-checkout", "--", url,
                    str(work), timeout_s=CLONE_TIMEOUT_S,
                )  # fmt: skip
            except GitFailed as exc:
                if any(sign in exc.stderr.lower() for sign in _NO_REPO):
                    raise RepoNotFound("GitHub has no public repository at that URL") from None
                raise
            yield work / ".git"

    async def _fetch(self, git_dir: Path, sha: str) -> None:
        """Fetch the commit itself, every time.

        A branch may no longer point at it (a force-push replaced it). Asking for it outright
        also gives one place to tell "the server has no such commit", a result for the team,
        from a network failure, which is retried. A partial clone would otherwise fetch it
        silently inside whichever command first touched it, with neither distinction.
        """
        try:
            await run_git(
                "fetch", "--quiet", "--filter=blob:none", "origin", sha,
                git_dir=git_dir, timeout_s=CLONE_TIMEOUT_S,
            )  # fmt: skip
        except GitFailed as exc:
            if any(sign in exc.stderr.lower() for sign in _NO_COMMIT):
                raise CommitNotFound(f"the repository has no commit {sha}") from None
            raise

    async def _get(self, path: str) -> dict[str, object]:
        try:
            response = await self._client.get(API + path, headers=self._headers)
        except httpx.HTTPError as exc:
            raise InfraError(f"GitHub could not be reached: {type(exc).__name__}") from exc
        if response.status_code == 200:
            data: dict[str, object] = response.json()
            return data
        if _rate_limited(response):
            raise InfraError(f"GitHub rate limit reached (HTTP {response.status_code})")
        if response.status_code in (403, 404, 451):
            raise RepoNotFound("GitHub has no public repository at that URL")
        raise InfraError(f"GitHub answered HTTP {response.status_code}")


def _comparison(data: dict[str, object], files: list[object]) -> Comparison:
    status = data.get("status")
    if status in ("behind", "identical"):
        return Comparison(status=status, lines_added=0, files_changed=0, commits=[])
    if status not in ("ahead", "diverged"):
        raise InfraError(f"GitHub answered a comparison with status {status!r}")
    lines = 0
    for entry in files:
        if not isinstance(entry, dict) or not isinstance(entry.get("filename"), str):
            raise InfraError("GitHub answered a comparison with a file entry it could not read")
        if counts_toward_lines(entry["filename"]):
            lines += int(entry.get("additions") or 0)
    raw_commits = data.get("commits")
    commits = []
    for entry in reversed(raw_commits if isinstance(raw_commits, list) else []):  # newest first
        commit = entry.get("commit", {}) if isinstance(entry, dict) else {}
        commits.append(
            {
                "sha": str(entry.get("sha", "")),
                "author_date": str(commit.get("author", {}).get("date", "")),
                "committer_date": str(commit.get("committer", {}).get("date", "")),
                "message": str(commit.get("message", "")).split("\n", 1)[0],
            }
        )
    return Comparison(
        status=status, lines_added=lines, files_changed=len(files), commits=commits[:HISTORY_LIMIT]
    )


def _rate_limited(response: httpx.Response) -> bool:
    if response.status_code not in (403, 429):
        return False
    return (
        response.status_code == 429
        or "retry-after" in response.headers
        or response.headers.get("x-ratelimit-remaining") == "0"
    )
