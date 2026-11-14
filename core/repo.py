"""The repository boundary (section 13.1, D-006).

All access to a team's repository goes through `RepoProvider`. This file holds the part
registration needs; `head`, `compare` and `snapshot` join it with the milestones that use them.
"""

from datetime import datetime
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


class RepoProvider(Protocol):
    name: ProviderName

    async def describe(self, repo_url: str) -> RepoInfo:
        """Look a repository up.

        Raises `InvalidRepoUrl`, `RepoNotFound`, or `InfraError` when the provider cannot
        be reached or is rate-limiting us.
        """
        ...
