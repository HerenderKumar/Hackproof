"""The container runtime boundary (section 3.3).

Everything the pipeline asks of Docker goes through `ContainerRuntime`, so unit tests use a
fake. Startup cleanup and the heartbeat use the listing and removal calls, builds look up
images, and runs create a network and start hardened containers on it (section 12.3).
"""

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

import docker
import docker.errors

# Every container and network of a run carries this label, set to the run's attempt ID.
ATTEMPT_LABEL = "hackproof.attempt"


@dataclass(frozen=True)
class Labelled:
    """A container or network, and the attempt that created it."""

    id: str
    attempt: str


# Section 12.3: what every run container gets, whatever else its spec says.
CAP_DROP = ["ALL"]
SECURITY_OPT = ["no-new-privileges"]
TMPFS = {"/tmp": "rw,size=256m"}


@dataclass(frozen=True)
class ContainerSpec:
    """A run container. Capabilities are always dropped, privileges never escalate, `/tmp` is
    a 256 MB tmpfs, swap equals `memory`, and the container carries the attempt label."""

    name: str
    image: str
    network: str
    attempt: str
    environment: dict[str, str]  # the whole environment Docker is given; nothing is inherited
    memory: str
    cpus: float | None = None
    pids_limit: int | None = None
    alias: str | None = None  # its name on the run network
    user: str | None = None  # None runs as the image's own user
    read_only: bool = True
    mounts: tuple[tuple[str, str], ...] = ()  # (host path, container path), read-write
    # Replaces the image's entrypoint and command together; None runs the image as built.
    command: tuple[str, ...] | None = None
    # The runner of a run with UI tests (M11, D-058): an init process to reap the browser's
    # children, a private /dev/shm of this size, and a seccomp profile's JSON text.
    init: bool = False
    shm_size: str | None = None
    seccomp: str | None = None
    # More networks the container joins before it starts, each with its alias there: the model
    # door is on its run's out network and on the run's internal one as `model-door`.
    extra_networks: tuple[tuple[str, str | None], ...] = ()


@dataclass(frozen=True)
class ContainerState:
    running: bool
    exit_code: int | None
    oom_killed: bool


class ContainerRuntime(Protocol):
    async def ping(self) -> bool: ...

    async def attempt_containers(self) -> list[Labelled]: ...

    async def attempt_networks(self) -> list[Labelled]: ...

    async def remove_container(self, container_id: str) -> None: ...

    async def remove_network(self, network_id: str) -> None: ...

    async def image_id(self, tag: str) -> str | None:
        """The image's ID (`docker image inspect --format '{{.Id}}'`), or None if absent."""
        ...

    async def is_running(self, name: str) -> bool:
        """Whether the container called `name` exists and is running."""
        ...

    async def create_network(self, name: str, attempt: str, *, internal: bool = True) -> str:
        """A labelled network, internal (no outside access) unless asked. Returns its ID."""
        ...

    async def start_container(self, spec: ContainerSpec) -> str:
        """Create and start a container from `spec`. Returns its ID."""
        ...

    async def container_state(self, container_id: str) -> ContainerState: ...

    async def container_logs(self, container_id: str, tail: int) -> str:
        """The last `tail` lines of stdout and stderr."""
        ...

    async def image_exposed_ports(self, image: str) -> list[int]:
        """TCP ports in the image's `ExposedPorts`, in the order the image lists them."""
        ...


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

    async def image_id(self, tag: str) -> str | None:
        def inspect() -> str | None:
            try:
                return str(self._api().inspect_image(tag)["Id"])
            except docker.errors.ImageNotFound:
                return None

        return await asyncio.to_thread(inspect)

    async def is_running(self, name: str) -> bool:
        def inspect() -> bool:
            try:
                return bool(self._api().inspect_container(name)["State"]["Running"])
            except docker.errors.NotFound:
                return False

        return await asyncio.to_thread(inspect)

    async def create_network(self, name: str, attempt: str, *, internal: bool = True) -> str:
        def create() -> str:
            network = self._api().create_network(
                name, driver="bridge", internal=internal, labels={ATTEMPT_LABEL: attempt}
            )
            return str(network["Id"])

        return await asyncio.to_thread(create)

    async def start_container(self, spec: ContainerSpec) -> str:
        def start() -> str:
            api = self._api()
            host_config = api.create_host_config(
                network_mode=spec.network,
                cap_drop=CAP_DROP,
                security_opt=[
                    *SECURITY_OPT,
                    *([f"seccomp={spec.seccomp}"] if spec.seccomp else []),
                ],
                read_only=spec.read_only,
                init=spec.init or None,
                shm_size=spec.shm_size,
                tmpfs=TMPFS,
                mem_limit=spec.memory,
                memswap_limit=spec.memory,
                nano_cpus=int(spec.cpus * 1e9) if spec.cpus else None,
                pids_limit=spec.pids_limit,
                binds={host: {"bind": inside, "mode": "rw"} for host, inside in spec.mounts},
            )
            endpoint = api.create_endpoint_config(aliases=[spec.alias] if spec.alias else None)
            container = api.create_container(
                spec.image,
                # Overriding the entrypoint also drops the image's CMD.
                entrypoint=list(spec.command) if spec.command is not None else None,
                name=spec.name,
                user=spec.user,
                environment=spec.environment,
                labels={ATTEMPT_LABEL: spec.attempt},
                host_config=host_config,
                networking_config=api.create_networking_config({spec.network: endpoint}),
            )
            container_id = str(container["Id"])
            for network, alias in spec.extra_networks:
                api.connect_container_to_network(
                    container_id, network, aliases=[alias] if alias else None
                )
            api.start(container_id)
            return container_id

        return await asyncio.to_thread(start)

    async def container_state(self, container_id: str) -> ContainerState:
        def inspect() -> ContainerState:
            state = self._api().inspect_container(container_id)["State"]
            running = bool(state["Running"])
            return ContainerState(
                running=running,
                exit_code=None if running else int(state["ExitCode"]),
                oom_killed=bool(state.get("OOMKilled")),
            )

        return await asyncio.to_thread(inspect)

    async def container_logs(self, container_id: str, tail: int) -> str:
        def read() -> str:
            raw = self._api().logs(container_id, stdout=True, stderr=True, tail=tail)
            return bytes(raw).decode(errors="replace")

        return await asyncio.to_thread(read)

    async def image_exposed_ports(self, image: str) -> list[int]:
        def inspect() -> list[int]:
            exposed = self._api().inspect_image(image)["Config"].get("ExposedPorts") or {}
            ports = []
            for key in exposed:
                number, _, protocol = key.partition("/")
                if protocol in ("", "tcp") and number.isdigit():
                    ports.append(int(number))
            return ports

        return await asyncio.to_thread(inspect)
