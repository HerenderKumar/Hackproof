"""Startup cleanup (section 11.4).

A worker that died mid-run leaves containers, networks and extracted sources behind. On start,
everything labelled with an attempt that is no longer a running job's current attempt goes,
and so do work directories older than an hour.
"""

import asyncio
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import text

from core.db.engine import Sessions
from core.logging import get_logger
from worker.runtime import ContainerRuntime, Labelled

WORK_DIR_MAX_AGE = timedelta(hours=1)

log = get_logger()


@dataclass(frozen=True)
class CleanupReport:
    containers: int
    networks: int
    work_dirs: int


async def startup_cleanup(
    sessions: Sessions, runtime: ContainerRuntime, work_root: Path, now: datetime
) -> CleanupReport:
    async with sessions() as session:
        rows = await session.execute(
            text("SELECT attempt_id FROM jobs WHERE status = 'running' AND attempt_id IS NOT NULL")
        )
        live = {str(attempt_id) for attempt_id in rows.scalars()}

    containers = networks = 0
    try:
        # Containers first: a network cannot be removed while a container is attached to it.
        containers = await _remove_dead(
            await runtime.attempt_containers(), live, runtime.remove_container, "container"
        )
        networks = await _remove_dead(
            await runtime.attempt_networks(), live, runtime.remove_network, "network"
        )
    except Exception:
        log.exception("cleanup_docker_unreachable")

    work_dirs = await asyncio.to_thread(_sweep_work_dirs, work_root, now)
    report = CleanupReport(containers=containers, networks=networks, work_dirs=work_dirs)
    log.info("startup_cleanup", containers=containers, networks=networks, work_dirs=work_dirs)
    return report


async def _remove_dead(
    items: list[Labelled], live: set[str], remove: Callable[[str], Awaitable[None]], what: str
) -> int:
    removed = 0
    for item in items:
        if item.attempt in live:
            continue
        try:
            await remove(item.id)
            removed += 1
        except Exception:
            log.warning("cleanup_remove_failed", what=what, id=item.id, exc_info=True)
    return removed


def _sweep_work_dirs(work_root: Path, now: datetime) -> int:
    if not work_root.is_dir():
        return 0
    cutoff = (now - WORK_DIR_MAX_AGE).timestamp()
    removed = 0
    for entry in work_root.iterdir():
        try:
            if entry.lstat().st_mtime > cutoff:
                continue
            if entry.is_symlink():
                entry.unlink()  # never follow a link out of the work directory
            elif entry.is_dir():
                shutil.rmtree(entry)
            else:
                continue
            removed += 1
        except OSError:
            log.warning("cleanup_remove_failed", what="work_dir", id=entry.name, exc_info=True)
    return removed
