"""The container runtime boundary (section 3.3).

Everything the pipeline asks of Docker goes through `ContainerRuntime`, so unit tests use a
fake. This is the part startup cleanup and the heartbeat need; builds and runs arrive in M4.
"""

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

import docker

# Every container and network of a run carries this label, set to the run's attempt ID.
ATTEMPT_LABEL = "hackproof.attempt"


@dataclass(frozen=True)
class Labelled:
    """A container or network, and the attempt that created it."""

    id: str
    attempt: str


class ContainerRuntime(Protocol):
    async def ping(self) -> bool: ...

    async def attempt_containers(self) -> list[Labelled]: ...

    async def attempt_networks(self) -> list[Labelled]: ...

    async def remove_container(self, container_id: str) -> None: ...

    async def remove_network(self, network_id: str) -> None: ...


class DockerRuntime:
    """The host's Docker daemon, through the socket mounted into the worker (section 8.1)."""

    def __init__(self) -> None:
        self._client: Any = None

    def _api(self) -> Any:
        # Connecting asks the daemon for its version, so it fails while Docker is down.
        # Connect on first use and keep nothing on failure, so a later call tries again.
        if self._client is None:
            self._client = docker.from_env(timeout=30)
        return self._client.api

    async def ping(self) -> bool:
        def ping() -> bool:
            try:
                return bool(self._api().ping())
            except Exception:
                self._client = None
                return False

        return await asyncio.to_thread(ping)

    async def attempt_containers(self) -> list[Labelled]:
        def find() -> list[Labelled]:
            found = self._api().containers(all=True, filters={"label": ATTEMPT_LABEL})
            return [Labelled(id=c["Id"], attempt=c["Labels"][ATTEMPT_LABEL]) for c in found]

        return await asyncio.to_thread(find)

    async def attempt_networks(self) -> list[Labelled]:
        def find() -> list[Labelled]:
            found = self._api().networks(filters={"label": ATTEMPT_LABEL})
            return [Labelled(id=n["Id"], attempt=n["Labels"][ATTEMPT_LABEL]) for n in found]

        return await asyncio.to_thread(find)

    async def remove_container(self, container_id: str) -> None:
        await asyncio.to_thread(self._api().remove_container, container_id, v=True, force=True)

    async def remove_network(self, network_id: str) -> None:
        await asyncio.to_thread(self._api().remove_network, network_id)
