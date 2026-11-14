"""Repository providers. `REPO_PROVIDER` chooses one for the whole deployment (D-006)."""

import httpx

from core.clock import Clock
from core.providers.github import GitHubProvider
from core.providers.local import LocalGitProvider
from core.repo import RepoProvider
from core.settings import Settings


def make_provider(settings: Settings, clock: Clock) -> RepoProvider:
    if settings.repo_provider == "local":
        return LocalGitProvider(settings.local_remotes_dir, clock)
    token = settings.github_token.get_secret_value() if settings.github_token else ""
    client = httpx.AsyncClient(timeout=20.0, follow_redirects=True)
    return GitHubProvider(token, client)
