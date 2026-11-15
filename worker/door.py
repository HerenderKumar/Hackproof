"""The model door's side of a run (Prune design, B2): its container, its networks, the app's
variables, and its log.

With `MODEL_DOOR=prune` a run gets a second network, `hp-<attempt>-out`, an ordinary bridge with
a way out, and the door (`door/`) on both: on the out network as itself, on the run's internal
network as `model-door`. The app stays on the internal network only, so its one way out is the
door, which forwards model calls to Prune and nothing else. The app's environment gains the
door's address for the OpenAI and Anthropic SDKs, a token made for this attempt in place of an
API key, and the allowed models; never the Prune key, which only the door holds.

The door writes `ready` and `log.jsonl` into a folder beside the run folder (not inside it, so
the runner's mount never holds it). The worker waits for `ready` before starting the app, reads
the log after the run, and removes the folder.
"""

import asyncio
import json
import os
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from core.errors import InfraError
from core.settings import Settings
from worker.runtime import ContainerSpec

DOOR_ALIAS = "model-door"
DOOR_PORT = 8080
DOOR_URL = f"http://{DOOR_ALIAS}:{DOOR_PORT}"
DOOR_DIR_IN_CONTAINER = "/door"
DOOR_USER = "10001:10001"
DOOR_MEMORY = "256m"
DOOR_CPUS = 0.5
DOOR_PIDS = 64
READY_TIMEOUT_S = 20.0
FEATURES = {"final": "final_run", "practice": "self_check_run"}


@dataclass(frozen=True)
class DoorTerms:
    """The event's terms for every run's door, from the settings."""

    image: str
    prune_base_url: str
    prune_api_key: str
    models: tuple[str, ...]
    run_budget_usd: float
    max_calls: int
    timeout_s: float

    @classmethod
    def from_settings(cls, settings: Settings) -> "DoorTerms | None":
        if settings.model_door != "prune":
            return None
        return cls(
            image=settings.door_image,
            prune_base_url=settings.prune_base_url,
            prune_api_key=settings.prune_key,
            models=settings.door_model_list,
            run_budget_usd=settings.door_run_budget_usd,
            max_calls=settings.door_max_calls,
            timeout_s=settings.door_timeout_s,
        )


@dataclass(frozen=True)
class DoorRun:
    """One attempt's door: who it is for, its token and its folder."""

    terms: DoorTerms
    attempt_id: UUID
    team_id: UUID
    feature: str
    token: str
    folder: Path

    @classmethod
    def make(
        cls, terms: DoorTerms, *, attempt_id: UUID, team_id: UUID, mode: str, run_dir: Path
    ) -> "DoorRun":
        return cls(
            terms=terms,
            attempt_id=attempt_id,
            team_id=team_id,
            feature=FEATURES[mode],
            token=secrets.token_urlsafe(24),
            folder=run_dir.with_name(f"{run_dir.name}-door"),
        )

    @property
    def run_id(self) -> str:
        return str(self.attempt_id)


def out_network_name(attempt_id: UUID) -> str:
    return f"hp-{attempt_id}-out"


def door_container(door: DoorRun, internal_network: str) -> ContainerSpec:
    t = door.terms
    environment = {
        "DOOR_TOKEN": door.token,
        "PRUNE_BASE_URL": t.prune_base_url,
        "PRUNE_API_KEY": t.prune_api_key,
        "DOOR_MODELS": ",".join(t.models),
        "DOOR_RUN_ID": door.run_id,
        "DOOR_RUN_BUDGET_USD": f"{t.run_budget_usd:g}",
        "DOOR_MAX_CALLS": str(t.max_calls),
        "DOOR_CUSTOMER_ID": f"team-{door.team_id}",
        "DOOR_FEATURE_ID": door.feature,
        "DOOR_TIMEOUT_S": f"{t.timeout_s:g}",
        "DOOR_LOG": f"{DOOR_DIR_IN_CONTAINER}/log.jsonl",
        "DOOR_READY": f"{DOOR_DIR_IN_CONTAINER}/ready",
    }
    return ContainerSpec(
        name=f"hp-{door.attempt_id}-door",
        image=t.image,
        network=out_network_name(door.attempt_id),
        attempt=str(door.attempt_id),
        environment=environment,
        memory=DOOR_MEMORY,
        cpus=DOOR_CPUS,
        pids_limit=DOOR_PIDS,
        user=DOOR_USER,
        read_only=True,
        mounts=((str(door.folder), DOOR_DIR_IN_CONTAINER),),
        extra_networks=((internal_network, DOOR_ALIAS),),
    )


def app_variables(door: DoorRun) -> dict[str, str]:
    """What the app's environment gains: the stock OpenAI and Anthropic SDKs read these."""
    return {
        "OPENAI_BASE_URL": f"{DOOR_URL}/v1",
        "OPENAI_API_KEY": door.token,
        "ANTHROPIC_BASE_URL": DOOR_URL,
        "ANTHROPIC_API_KEY": door.token,
        "HP_MODELS": ",".join(door.terms.models),
    }


def prepare_folder(door: DoorRun) -> None:
    shutil.rmtree(door.folder, ignore_errors=True)
    door.folder.mkdir(parents=True)
    os.chmod(door.folder, 0o777)  # the door runs as its own user, not the worker's


async def wait_ready(door: DoorRun, is_running: Any, poll_s: float = 0.2) -> None:
    """Until the door says it is listening. A door that stops or never answers is ours to fix:
    an infrastructure error, retried, never a team's failure."""
    deadline = time.monotonic() + READY_TIMEOUT_S
    ready = door.folder / "ready"
    while not await asyncio.to_thread(ready.exists):
        if not await is_running() or time.monotonic() > deadline:
            raise InfraError("the model door did not start")
        await asyncio.sleep(poll_s)


def read_log(door: DoorRun) -> list[dict[str, Any]]:
    path = door.folder / "log.jsonl"
    if not path.exists():
        return []
    lines = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            lines.append(item)
    return lines


def remove_folder(door: DoorRun) -> None:
    shutil.rmtree(door.folder, ignore_errors=True)
