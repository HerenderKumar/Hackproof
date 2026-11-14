"""The worker's heartbeat (D-030): ping Docker, remember the answer, publish it for `/readyz`."""

import asyncio
from dataclasses import dataclass
from pathlib import Path

from core import worker_health
from core.clock import Clock
from core.logging import get_logger
from core.worker_health import WorkerHealth
from worker.runtime import ContainerRuntime

INTERVAL_S = 15.0

log = get_logger()


@dataclass
class DockerState:
    """Whether the last heartbeat reached Docker. The heavy lane claims nothing while it did not."""

    reachable: bool = False


async def beat(
    runtime: ContainerRuntime, artifact_dir: Path, clock: Clock, state: DockerState
) -> None:
    try:
        reachable = await runtime.ping()
    except Exception:
        reachable = False
    if reachable != state.reachable:
        log.info("docker_reachable" if reachable else "docker_unreachable")
    state.reachable = reachable
    health = WorkerHealth(at=clock.now(), docker_ok=reachable)
    await asyncio.to_thread(worker_health.write, artifact_dir, health)
