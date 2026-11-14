"""Fakes for everything external (section 3.3). Unit tests never touch Docker or the network."""

from datetime import UTC, datetime, timedelta

from core.repo import ProviderName, RepoInfo, RepoNotFound
from worker.runtime import Labelled


class FakeClock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def now(self) -> datetime:
        return self.at

    def advance(self, **delta: float) -> None:
        self.at += timedelta(**delta)


class FakeRuntime:
    """An in-memory ContainerRuntime: labelled containers and networks, as `{id: attempt}`."""

    def __init__(
        self,
        *,
        containers: dict[str, str] | None = None,
        networks: dict[str, str] | None = None,
        reachable: bool = True,
        unremovable: set[str] | None = None,
    ) -> None:
        self.containers = dict(containers or {})
        self.networks = dict(networks or {})
        self.reachable = reachable
        self.unremovable = unremovable or set()
        self.removed: list[str] = []

    def _require_daemon(self) -> None:
        if not self.reachable:
            raise ConnectionError("cannot connect to the Docker daemon")

    async def ping(self) -> bool:
        return self.reachable

    async def attempt_containers(self) -> list[Labelled]:
        self._require_daemon()
        return [Labelled(id=i, attempt=a) for i, a in self.containers.items()]

    async def attempt_networks(self) -> list[Labelled]:
        self._require_daemon()
        return [Labelled(id=i, attempt=a) for i, a in self.networks.items()]

    async def remove_container(self, container_id: str) -> None:
        self._remove(self.containers, container_id)

    async def remove_network(self, network_id: str) -> None:
        self._remove(self.networks, network_id)

    def _remove(self, items: dict[str, str], item_id: str) -> None:
        self._require_daemon()
        if item_id in self.unremovable:
            raise RuntimeError(f"{item_id} is in use")
        del items[item_id]
        self.removed.append(item_id)


class FakeRepoProvider:
    """A RepoProvider over a dictionary: `{url: RepoInfo or the error to raise}`."""

    name: ProviderName = "github"

    def __init__(self) -> None:
        self.repos: dict[str, RepoInfo | Exception] = {}
        self.calls: list[str] = []

    def add(self, url: str, /, **overrides: object) -> RepoInfo:
        values: dict[str, object] = {
            "provider": self.name,
            "id": str(1000 + len(self.repos)),
            "url": url,
            "default_branch": "main",
            "created_at": datetime(2026, 11, 1, tzinfo=UTC),
            "is_public": True,
        }
        info = RepoInfo(**{**values, **overrides})  # type: ignore[arg-type]
        self.repos[url] = info
        return info

    def fail(self, url: str, error: Exception) -> None:
        self.repos[url] = error

    async def describe(self, repo_url: str) -> RepoInfo:
        self.calls.append(repo_url)
        found = self.repos.get(repo_url.strip())
        if found is None:
            raise RepoNotFound(repo_url)
        if isinstance(found, Exception):
            raise found
        return found
