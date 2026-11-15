"""Fakes for everything external (section 3.3). Unit tests never touch Docker or the network."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.repo import Comparison, ProviderName, RepoInfo, RepoNotFound
from worker.build import CappedLog
from worker.runtime import ContainerSpec, ContainerState, Labelled


class FakeClock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def now(self) -> datetime:
        return self.at

    def advance(self, **delta: float) -> None:
        self.at += timedelta(**delta)


class FakeRuntime:
    """An in-memory ContainerRuntime: labelled containers and networks, as `{id: attempt}`.

    For runs, `on_start(runtime, container_id, spec)` is called as each container starts, so a
    test can play the app or the runner: write files into a mount, `exit` the container, or set
    its `logs`. A started container keeps running until something calls `exit`.
    """

    def __init__(
        self,
        *,
        containers: dict[str, str] | None = None,
        networks: dict[str, str] | None = None,
        reachable: bool = True,
        unremovable: set[str] | None = None,
        images: dict[str, str] | None = None,
        running: set[str] | None = None,
    ) -> None:
        self.containers = dict(containers or {})
        self.networks = dict(networks or {})
        self.images = dict(images or {})  # {tag: image ID}
        self.running = set(running or ())  # names of running containers
        self.reachable = reachable
        self.unremovable = unremovable or set()
        self.removed: list[str] = []
        # Runs
        self.specs: dict[str, ContainerSpec] = {}  # every container ever started, in order
        self.states: dict[str, ContainerState] = {}
        self.logs: dict[str, str] = {}
        self.log_tails: list[int] = []
        self.exposed: dict[str, list[int]] = {}  # {image: ports}
        self.network_names: dict[str, str] = {}
        self.network_internal: dict[str, bool] = {}
        self.on_start: Callable[[FakeRuntime, str, ContainerSpec], None] | None = None
        self.fail_start: set[str] = set()  # images whose containers fail to start
        self._next_id = 0

    def _new_id(self, prefix: str) -> str:
        self._next_id += 1
        return f"{prefix}-{self._next_id}"

    def exit(self, container_id: str, code: int = 0, *, oom: bool = False) -> None:
        self.states[container_id] = ContainerState(running=False, exit_code=code, oom_killed=oom)

    def started(self, *, app: bool) -> list[ContainerSpec]:
        return [s for s in self.specs.values() if (s.alias == "app") == app]

    async def create_network(self, name: str, attempt: str, *, internal: bool = True) -> str:
        self._require_daemon()
        network_id = self._new_id("net")
        self.networks[network_id] = attempt
        self.network_names[network_id] = name
        self.network_internal[network_id] = internal
        return network_id

    async def start_container(self, spec: ContainerSpec) -> str:
        self._require_daemon()
        if spec.image in self.fail_start:
            raise RuntimeError(f"cannot start {spec.image}")
        container_id = self._new_id("ctr")
        self.containers[container_id] = spec.attempt
        self.specs[container_id] = spec
        self.states[container_id] = ContainerState(running=True, exit_code=None, oom_killed=False)
        if self.on_start is not None:
            self.on_start(self, container_id, spec)
        return container_id

    async def container_state(self, container_id: str) -> ContainerState:
        self._require_daemon()
        return self.states[container_id]

    async def container_logs(self, container_id: str, tail: int) -> str:
        self._require_daemon()
        self.log_tails.append(tail)
        lines = self.logs.get(container_id, "").splitlines(keepends=True)
        return "".join(lines[-tail:])

    async def image_exposed_ports(self, image: str) -> list[int]:
        self._require_daemon()
        return list(self.exposed.get(image, []))

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

    async def image_id(self, tag: str) -> str | None:
        self._require_daemon()
        return self.images.get(tag)

    async def is_running(self, name: str) -> bool:
        self._require_daemon()
        return name in self.running

    def _remove(self, items: dict[str, str], item_id: str) -> None:
        self._require_daemon()
        if item_id in self.unremovable:
            raise RuntimeError(f"{item_id} is in use")
        del items[item_id]
        self.removed.append(item_id)


@dataclass(frozen=True)
class CommandCall:
    argv: list[str]
    env: dict[str, str]
    deadline: float


@dataclass(frozen=True)
class _Scripted:
    exit_code: int | None
    output: str
    plan: object
    info: object


class FakeCommands:
    """A `worker.build.CommandRunner` that answers from a script instead of running anything.

    `on("railpack build", exit_code=1, output=...)` scripts the command whose first two words
    match; `exit_code=None` stands for a command killed at its deadline. A scripted
    `railpack prepare` writes `plan` and `info` where its `--plan-out` and `--info-out` point.
    """

    def __init__(self) -> None:
        self.script: dict[str, _Scripted] = {}
        self.calls: list[CommandCall] = []

    def on(
        self,
        step: str,
        *,
        exit_code: int | None = 0,
        output: str = "",
        plan: object = None,
        info: object = None,
    ) -> None:
        self.script[step] = _Scripted(exit_code, output, plan, info)

    async def __call__(
        self, argv: list[str], *, env: dict[str, str], deadline: float, log: CappedLog
    ) -> int | None:
        self.calls.append(CommandCall(list(argv), dict(env), deadline))
        step = " ".join(argv[:2])
        scripted = self.script.get(step)
        if scripted is None:
            raise AssertionError(f"unexpected command: {argv}")
        log.write(scripted.output.encode())
        for flag, content in (("--plan-out", scripted.plan), ("--info-out", scripted.info)):
            if flag in argv and content is not None:
                Path(argv[argv.index(flag) + 1]).write_text(json.dumps(content))
        return scripted.exit_code


class FakeRepoProvider:
    """A RepoProvider over a dictionary: `{url: RepoInfo or the error to raise}`."""

    name: ProviderName = "github"

    def __init__(self) -> None:
        self.repos: dict[str, RepoInfo | Exception] = {}
        self.calls: list[str] = []
        # Each repository's default-branch head by URL: a SHA, None (no commit), or an error.
        self.heads: dict[str, str | Exception | None] = {}
        # `compare` answers by (base, head): a Comparison or the error to raise.
        self.comparisons: dict[tuple[str | None, str], Comparison | Exception] = {}

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

    async def head(self, repo: RepoInfo) -> str | None:
        found = self.heads.get(repo.url)
        if isinstance(found, Exception):
            raise found
        return found

    async def compare(self, repo: RepoInfo, base: str | None, head: str) -> Comparison:
        found = self.comparisons.get((base, head))
        if found is None:
            raise AssertionError(f"no comparison set for {(base, head)}")
        if isinstance(found, Exception):
            raise found
        return found
