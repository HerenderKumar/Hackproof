"""Public GitHub repositories through the REST API (D-005). No GitHub App, no webhooks."""

import re
from urllib.parse import urlsplit

import httpx

from core.errors import InfraError
from core.repo import InvalidRepoUrl, ProviderName, RepoInfo, RepoNotFound

API = "https://api.github.com"
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

    def __init__(self, token: str, client: httpx.AsyncClient) -> None:
        self._client = client
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


def _rate_limited(response: httpx.Response) -> bool:
    if response.status_code not in (403, 429):
        return False
    return (
        response.status_code == 429
        or "retry-after" in response.headers
        or response.headers.get("x-ratelimit-remaining") == "0"
    )
