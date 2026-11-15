"""Runs (section 12.3): start a built app and the runner on a network of their own.

Each run gets `hp-<attempt>`, an internal network with no route out. The app joins it as
`app`, locked down (fixed non-root user, read-only root, no capabilities, memory, CPU and
process caps) with three environment variables and nothing else. The runner joins it with the
run folder mounted at `/work`, reads `spec.json` there, and writes `startup.json` as soon as it
knows whether the app is up, then `results.json`.

While the runner works, the worker watches the app: an app that exits before answering ends
the run at once instead of leaving the runner to wait out the startup timeout. A runner that
exits without results is an infrastructure error (section 14.4). `RUN_TIMEOUT_S` bounds the
whole run, both passes included.

Fallbacks, at most once each, recorded in `flags_used`:
- the startup check failed and the app's log shows a permission or read-only error: start the
  app again as the image's own user with a writable root, every other restriction kept;
- 8080 may not be the app's port: the ports the image exposes are candidates after 8080, and
  the runner reports which one answered.

Whatever happens, both containers and the network are removed before this returns.
"""

import asyncio
import json
import re
import shutil
import time
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from core.errors import InfraError
from core.logging import get_logger
from core.settings import Settings
from worker.door import (
    DoorRun,
    app_variables,
    door_container,
    out_network_name,
    prepare_folder,
    read_log,
    remove_folder,
    wait_ready,
)
from worker.media import Media, collect
from worker.runtime import ContainerRuntime, ContainerSpec, ContainerState

APP_PORT = 8080
APP_ALIAS = "app"
APP_USER = "10001:10001"
APP_ENV = {"PORT": str(APP_PORT), "HOST": "0.0.0.0", "NODE_ENV": "production"}
BASE_URL = f"http://{APP_ALIAS}:{APP_PORT}"
APP_LOG_LINES = 200
RUN_DIR_IN_RUNNER = "/work"
RUNNER_SHM = "512m"
SECCOMP_PROFILE = Path(__file__).resolve().parents[1] / "runner" / "seccomp_profile.json"

_PERMISSION_PROBLEM = re.compile(
    r"EACCES|EROFS|permission denied|read-only file system", re.IGNORECASE
)

log = get_logger()


@dataclass(frozen=True)
class SandboxLimits:
    runner_image: str
    app_memory: str
    app_cpus: float
    app_pids_limit: int
    runner_memory: str
    startup_timeout_s: int
    run_timeout_s: float
    runner_pids_limit: int = 512
    runner_cpus: float = 2.0
    browser_sandbox: bool = True

    @classmethod
    def from_settings(cls, settings: Settings) -> "SandboxLimits":
        return cls(
            runner_pids_limit=settings.runner_pids_limit,
            runner_cpus=settings.runner_cpus,
            browser_sandbox=settings.runner_browser_sandbox,
            runner_image=settings.runner_image,
            app_memory=settings.app_memory,
            app_cpus=settings.app_cpus,
            app_pids_limit=settings.app_pids_limit,
            runner_memory=settings.runner_memory,
            startup_timeout_s=settings.startup_timeout_s,
            run_timeout_s=settings.run_timeout_s,
        )


@dataclass(frozen=True)
class RunOutcome:
    startup_status: Literal["ok", "fail"]
    startup_detail: str
    flags_used: dict[str, Any]
    results: dict[str, Any] | None  # the runner's results.json; None if it did not finish
    app_log_path: Path
    timed_out: bool
    media: Media = field(default_factory=dict)  # the UI files kept (worker/media.py)
    # With the model door on: its log, one line per call the app made (worker/door.py).
    door_log: tuple[dict[str, Any], ...] | None = None


@dataclass(frozen=True)
class _Pass:
    startup: dict[str, Any]
    results: dict[str, Any] | None
    app_id: str
    timed_out: bool


async def run_sandboxed(
    runtime: ContainerRuntime,
    *,
    image: str,
    attempt_id: UUID,
    run_dir: Path,
    mode: Literal["practice", "final"],
    specs: list[dict[str, Any]],
    limits: SandboxLimits,
    app_log_path: Path,
    poll_s: float = 0.5,
    door: DoorRun | None = None,
) -> RunOutcome:
    attempt = str(attempt_id)
    network = network_name(attempt_id)
    browser = any(spec.get("kind") == "ui" for spec in specs)
    deadline = time.monotonic() + limits.run_timeout_s
    started: list[str] = []
    door_started: list[str] = []  # kept apart: the permission fallback restarts only the app
    network_ids: list[str] = []
    extra_env = app_variables(door) if door is not None else {}

    async def one_pass(strict: bool) -> _Pass:
        app = app_container(image, attempt_id, limits, strict=strict, extra_env=extra_env)
        app_id = await _start(runtime, started, app)
        runner = runner_container(attempt_id, run_dir, limits, browser=browser)
        runner_id = await _start(runtime, started, runner)
        return await _watch(runtime, app_id, runner_id, run_dir, deadline, limits, poll_s)

    try:
        exposed = await runtime.image_exposed_ports(image)
        candidates = [APP_PORT, *dict.fromkeys(p for p in exposed if p != APP_PORT)]
        request = {
            "mode": mode,
            "base_url": BASE_URL,
            "candidate_ports": candidates,
            "startup_timeout_s": limits.startup_timeout_s,
            "specs": specs,
        }
        await asyncio.to_thread(_prepare_run_dir, run_dir, request)
        network_ids.append(await runtime.create_network(network, attempt))
        if door is not None:
            await _start_door(runtime, door, network, network_ids, door_started)

        result = await one_pass(strict=True)
        root_fallback = False
        if result.startup.get("status") != "ok" and not result.timed_out:
            app_log = await runtime.container_logs(result.app_id, APP_LOG_LINES)
            if _PERMISSION_PROBLEM.search(app_log):
                log.info("run_permission_fallback")
                await _remove_containers(runtime, started)
                await asyncio.to_thread(_prepare_run_dir, run_dir, request)
                result = await one_pass(strict=False)
                root_fallback = True

        app_log = await runtime.container_logs(result.app_id, APP_LOG_LINES)
        await asyncio.to_thread(_save_text, app_log_path, app_log)
    finally:
        await _remove_containers(runtime, [*started, *door_started])
        for network_id in reversed(network_ids):
            try:
                await runtime.remove_network(network_id)
            except Exception:
                log.warning("run_cleanup_failed", what="network", id=network_id, exc_info=True)
        door_log = None
        if door is not None:
            door_log = tuple(await asyncio.to_thread(read_log, door))
            await asyncio.to_thread(remove_folder, door)

    flags: dict[str, Any] = {}
    collected = None
    if browser:
        destination = app_log_path.parent / "ui"
        collected = await asyncio.to_thread(collect, run_dir, specs, mode, destination)
        flags = {"browser_sandbox": limits.browser_sandbox, "media_dropped": collected.dropped}

    ok = result.startup.get("status") == "ok"
    outcome = RunOutcome(
        startup_status="ok" if ok else "fail",
        startup_detail=str(result.startup.get("detail", "")),
        flags_used={
            "root_fallback": root_fallback,
            "candidate_ports": candidates,
            "port": result.startup.get("port") if ok else None,
            **flags,
        },
        results=result.results,
        app_log_path=app_log_path,
        timed_out=result.timed_out,
        media=collected.media if collected is not None else {},
        door_log=door_log,
    )
    log.info(
        "run_finished",
        startup_status=outcome.startup_status,
        root_fallback=root_fallback,
        port=outcome.flags_used["port"],
        timed_out=outcome.timed_out,
    )
    return outcome


def network_name(attempt_id: UUID) -> str:
    return f"hp-{attempt_id}"


def app_container(
    image: str,
    attempt_id: UUID,
    limits: SandboxLimits,
    *,
    strict: bool = True,
    extra_env: dict[str, str] | None = None,
) -> ContainerSpec:
    """The team's app. `strict=False` is the permission fallback: the image's own user and a
    writable root filesystem, every other restriction kept. `extra_env` is the model door's
    address, token and models when the door is on."""
    network = network_name(attempt_id)
    return ContainerSpec(
        name=f"{network}-app",
        image=image,
        network=network,
        attempt=str(attempt_id),
        environment={**(extra_env or {}), **APP_ENV},
        memory=limits.app_memory,
        cpus=limits.app_cpus,
        pids_limit=limits.app_pids_limit,
        alias=APP_ALIAS,
        user=APP_USER if strict else None,
        read_only=strict,
    )


def runner_container(
    attempt_id: UUID, run_dir: Path, limits: SandboxLimits, *, browser: bool = False
) -> ContainerSpec:
    """The test runner, as the runner image's own non-root user, with the run folder at /work.

    With `browser` (the run carries a UI test) it also gets an init process, a private 512 MB
    /dev/shm, more processes, its own CPUs and the seccomp profile that lets Chromium sandbox
    itself (D-058). Every other restriction is the same."""
    network = network_name(attempt_id)
    environment = {"BASE_URL": BASE_URL}
    if browser:
        environment["RUNNER_BROWSER_SANDBOX"] = "true" if limits.browser_sandbox else "false"
    return ContainerSpec(
        name=f"{network}-runner",
        image=limits.runner_image,
        network=network,
        attempt=str(attempt_id),
        environment=environment,
        memory=limits.runner_memory,
        cpus=limits.runner_cpus if browser else None,
        pids_limit=limits.runner_pids_limit if browser else limits.app_pids_limit,
        mounts=((str(run_dir), RUN_DIR_IN_RUNNER),),
        init=browser,
        shm_size=RUNNER_SHM if browser else None,
        seccomp=seccomp_profile() if browser else None,
    )


@cache
def seccomp_profile() -> str:
    """The runner's seccomp profile as compact JSON: the Docker API takes the text, not a path."""
    return json.dumps(json.loads(SECCOMP_PROFILE.read_text()), separators=(",", ":"))


# --- helpers -------------------------------------------------------------------------------------


async def _start_door(
    runtime: ContainerRuntime,
    door: DoorRun,
    internal_network: str,
    network_ids: list[str],
    door_started: list[str],
) -> None:
    """The run's out network and its door, listening before the app starts."""
    await asyncio.to_thread(prepare_folder, door)
    out = out_network_name(door.attempt_id)
    network_ids.append(await runtime.create_network(out, str(door.attempt_id), internal=False))
    try:
        door_id = await _start(runtime, door_started, door_container(door, internal_network))
    except InfraError:
        raise
    except Exception as error:
        raise InfraError(f"the model door did not start: {error}") from error

    async def running() -> bool:
        return (await runtime.container_state(door_id)).running

    await wait_ready(door, running)
    log.info("door_ready", models=list(door.terms.models), max_calls=door.terms.max_calls)


async def _start(runtime: ContainerRuntime, started: list[str], spec: ContainerSpec) -> str:
    container_id = await runtime.start_container(spec)
    started.append(container_id)
    return container_id


async def _watch(
    runtime: ContainerRuntime,
    app_id: str,
    runner_id: str,
    run_dir: Path,
    deadline: float,
    limits: SandboxLimits,
    poll_s: float,
) -> _Pass:
    """Wait for the runner, ending early if the app dies before answering."""
    while True:
        runner = await runtime.container_state(runner_id)
        if not runner.running:
            results = await asyncio.to_thread(_read_json, run_dir / "results.json")
            if results is None:
                raise InfraError(
                    f"the runner exited with code {runner.exit_code} without writing results"
                )
            return _Pass(results["startup"], results, app_id, timed_out=False)

        startup = await asyncio.to_thread(_read_json, run_dir / "startup.json")
        if startup is None:
            app = await runtime.container_state(app_id)
            if not app.running:
                failed = {"status": "fail", "detail": _exited_before_answering(app)}
                return _Pass(failed, None, app_id, timed_out=False)

        if time.monotonic() >= deadline:
            late = f"the run timed out after {limits.run_timeout_s:g} s before the app answered"
            return _Pass(startup or {"status": "fail", "detail": late}, None, app_id, True)
        await asyncio.sleep(poll_s)


def _exited_before_answering(state: ContainerState) -> str:
    memory = " (out of memory)" if state.oom_killed else ""
    return f"the app exited with code {state.exit_code}{memory} before answering"


async def _remove_containers(runtime: ContainerRuntime, started: list[str]) -> None:
    while started:
        container_id = started.pop()
        try:
            await runtime.remove_container(container_id)
        except Exception:
            log.warning("run_cleanup_failed", what="container", id=container_id, exc_info=True)


def _prepare_run_dir(run_dir: Path, request: dict[str, Any]) -> None:
    """An empty run folder holding only `spec.json`, writable by the runner's own user."""
    run_dir.mkdir(parents=True, exist_ok=True)
    for entry in run_dir.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    spec = run_dir / "spec.json"
    spec.write_text(json.dumps(request))
    spec.chmod(0o644)
    run_dir.chmod(0o777)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        content = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return content if isinstance(content, dict) else None


def _save_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
