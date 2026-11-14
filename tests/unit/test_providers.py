"""`RepoProvider.describe` for GitHub (mocked HTTP) and local bare repos (real git)."""

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from core.errors import InfraError
from core.providers import make_provider
from core.providers.github import GitHubProvider
from core.providers.local import LocalGitProvider
from core.repo import InvalidRepoUrl, RepoInfo, RepoNotFound
from core.settings import Settings
from tests.fakes import FakeClock

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)

# --- GitHub ----------------------------------------------------------------------------------

REPO_JSON = {
    "id": 4242,
    "full_name": "acme/Rocket",
    "private": False,
    "default_branch": "trunk",
    "created_at": "2026-10-01T08:30:00Z",
}


def github(handler: httpx.MockTransport | None = None) -> GitHubProvider:
    def default(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=REPO_JSON)

    transport = handler or httpx.MockTransport(default)
    return GitHubProvider("gh-token", httpx.AsyncClient(transport=transport))


def responding(status: int, headers: dict[str, str] | None = None) -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda _: httpx.Response(status, json={"message": "x"}, headers=headers or {})
    )


async def test_github_describes_a_public_repository() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=REPO_JSON)

    info = await github(httpx.MockTransport(handler)).describe("https://github.com/acme/rocket")

    assert info == RepoInfo(
        provider="github",
        id="4242",
        url="https://github.com/acme/Rocket",
        default_branch="trunk",
        created_at=datetime(2026, 10, 1, 8, 30, tzinfo=UTC),
        is_public=True,
    )
    [request] = seen
    assert str(request.url) == "https://api.github.com/repos/acme/rocket"
    assert request.headers["authorization"] == "Bearer gh-token"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme/rocket.git",
        "https://github.com/acme/rocket/",
        "http://www.github.com/acme/rocket",
        "  https://github.com/acme/rocket  ",
    ],
)
async def test_github_accepts_the_usual_spellings_of_a_repository_url(url: str) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=REPO_JSON)

    await github(httpx.MockTransport(handler)).describe(url)

    assert seen == ["https://api.github.com/repos/acme/rocket"]


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.com/acme/rocket",
        "https://github.com/acme",
        "https://github.com/acme/rocket/tree/main",
        "https://github.com.evil.example/acme/rocket",
        "https://evil.example/github.com/acme/rocket",
        "git@github.com:acme/rocket.git",
        "https://github.com/acme/ro cket",
        "https://github.com/../rocket",
        "",
    ],
)
async def test_github_refuses_urls_that_are_not_a_repository_page(url: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no request should be sent for {url!r}")

    with pytest.raises(InvalidRepoUrl):
        await github(httpx.MockTransport(handler)).describe(url)


async def test_github_reports_a_private_repository_as_not_public() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={**REPO_JSON, "private": True})
    )
    info = await github(transport).describe("https://github.com/acme/rocket")
    assert info.is_public is False


async def test_github_404_means_the_repository_was_not_found() -> None:
    with pytest.raises(RepoNotFound):
        await github(responding(404)).describe("https://github.com/acme/rocket")


@pytest.mark.parametrize(
    ("status", "headers"),
    [
        (403, {"x-ratelimit-remaining": "0"}),
        (429, {"retry-after": "30"}),
        (500, {}),
        (502, {}),
        (401, {}),
    ],
)
async def test_github_rate_limits_and_outages_are_infrastructure_errors(
    status: int, headers: dict[str, str]
) -> None:
    with pytest.raises(InfraError):
        await github(responding(status, headers)).describe("https://github.com/acme/rocket")


async def test_github_network_failures_are_infrastructure_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with pytest.raises(InfraError):
        await github(httpx.MockTransport(handler)).describe("https://github.com/acme/rocket")


async def test_github_403_without_rate_limit_headers_is_treated_as_not_found() -> None:
    # GitHub answers 403 for repositories it will not show (blocked, disabled).
    with pytest.raises(RepoNotFound):
        await github(responding(403)).describe("https://github.com/acme/rocket")


# --- local bare repos ------------------------------------------------------------------------


def git(cwd: Path, *args: str, date: str = "2026-11-01T10:00:00+00:00") -> None:
    env = {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_DATE": date,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    }
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)


@pytest.fixture
def remotes(tmp_path: Path) -> Path:
    """A remotes directory holding `demo.git` (two commits on `trunk`) and `empty.git`."""
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-b", "trunk")
    (work / "README.md").write_text("# demo\n")
    git(work, "add", ".")
    git(work, "commit", "-m", "first", date="2026-11-01T10:00:00+00:00")
    (work / "app.py").write_text("print('hi')\n")
    git(work, "add", ".")
    git(work, "commit", "-m", "second", date="2026-11-02T10:00:00+00:00")
    root = tmp_path / "remotes"
    root.mkdir()
    git(tmp_path, "clone", "--bare", str(work), str(root / "demo.git"))
    git(root, "init", "--bare", "-b", "main", "empty.git")
    return root


async def test_local_describes_a_bare_repository(remotes: Path) -> None:
    info = await LocalGitProvider(remotes, FakeClock(NOW)).describe("local://demo")

    assert info == RepoInfo(
        provider="local",
        id="demo",
        url="local://demo",
        default_branch="trunk",
        created_at=datetime(2026, 11, 1, 10, 0, tzinfo=UTC),
        is_public=True,
    )


async def test_local_accepts_a_bare_name(remotes: Path) -> None:
    info = await LocalGitProvider(remotes, FakeClock(NOW)).describe("demo")
    assert (info.id, info.url) == ("demo", "local://demo")


async def test_local_dates_an_empty_repository_from_the_clock(remotes: Path) -> None:
    info = await LocalGitProvider(remotes, FakeClock(NOW)).describe("local://empty")
    assert (info.default_branch, info.created_at) == ("main", NOW)


async def test_local_reports_an_unknown_repository(remotes: Path) -> None:
    with pytest.raises(RepoNotFound):
        await LocalGitProvider(remotes, FakeClock(NOW)).describe("local://missing")


@pytest.mark.parametrize(
    "url", ["local://../demo", "local://a/b", "local://", "../demo", "/etc/passwd", "demo.git/.."]
)
async def test_local_refuses_names_that_could_leave_the_remotes_directory(
    remotes: Path, url: str
) -> None:
    with pytest.raises(InvalidRepoUrl):
        await LocalGitProvider(remotes, FakeClock(NOW)).describe(url)


# --- choosing a provider ---------------------------------------------------------------------


def test_the_configured_provider_is_built(tmp_path: Path) -> None:
    def settings(**overrides: object) -> Settings:
        return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]

    local = make_provider(
        settings(repo_provider="local", local_remotes_dir=tmp_path), FakeClock(NOW)
    )
    hosted = make_provider(settings(repo_provider="github", github_token="t"), FakeClock(NOW))

    assert isinstance(local, LocalGitProvider)
    assert isinstance(hosted, GitHubProvider)
