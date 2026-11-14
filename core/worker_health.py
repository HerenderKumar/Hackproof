"""The worker's heartbeat file (D-030).

Only the worker can reach Docker, so it writes what it sees to the artifact directory, which
the API also mounts. `/readyz` reads the file instead of holding the Docker socket.
"""

import os
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ValidationError

FILE_NAME = "worker_health.json"
MAX_AGE = timedelta(seconds=60)


class WorkerHealth(BaseModel):
    at: datetime
    docker_ok: bool


def write(artifact_dir: Path, health: WorkerHealth) -> None:
    """Replace the file in one step, so a reader never sees half a write."""
    target = artifact_dir / FILE_NAME
    scratch = target.with_suffix(".tmp")
    scratch.write_text(health.model_dump_json())
    os.replace(scratch, target)


def read(artifact_dir: Path) -> WorkerHealth | None:
    try:
        return WorkerHealth.model_validate_json((artifact_dir / FILE_NAME).read_text())
    except (OSError, ValidationError):
        return None


def docker_ready(health: WorkerHealth | None, now: datetime) -> bool:
    """True only if a live worker reached Docker within the last minute."""
    return health is not None and health.docker_ok and now - health.at <= MAX_AGE
