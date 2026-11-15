"""`make seed-demo`: create the demo event of section 22 and pre-run every job (M12).

The seed is one process playing the organiser, five teams and the worker. It calls the API
in-process (the routes the pages call) and runs the worker's own job handlers, both on one clock
that it holds at the kickoff, five minutes later and an hour later, then lets run. Model replies
come from recordings (`fixtures/llm`, the demo's in `fixtures/llm/demo/`); `--mode record` calls
the gateway for what has none and saves it in `fixtures/llm/demo/`.

The worker service must not run meanwhile, or it takes the seed's jobs and dates them with the
real clock: the make target stops it and starts it again afterwards.
"""

import asyncio
import time
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import text

from core import queue
from core.db.engine import Sessions
from core.llm import (
    ReplayMissing,
    Transport,
    live_transport,
    recording_transport,
    replay_transport,
)
from core.settings import Settings
from worker import lanes

Mode = Literal["replay", "record"]
LANES: tuple[queue.Lane, ...] = ("watch", "heavy")


class SeedError(Exception):
    """The seed could not go on; the message says what it was doing."""


class SeedClock:
    """The real time, until frozen at a moment; `live()` lets it run again."""

    def __init__(self) -> None:
        self._at: datetime | None = None

    def now(self) -> datetime:
        return self._at if self._at is not None else datetime.now(UTC)

    def freeze(self, at: datetime) -> None:
        self._at = at

    def live(self) -> None:
        self._at = None


_WAITING = text(
    """
    SELECT kind, count(*) AS n FROM jobs
    WHERE team_id = ANY(CAST(:teams AS uuid[])) AND status IN ('queued', 'running')
    GROUP BY kind ORDER BY kind
    """
)


async def drain(
    sessions: Sessions,
    handlers: Mapping[str, lanes.Handler],
    teams: Sequence[uuid.UUID],
    *,
    timeout_s: float = 1800.0,
    idle_s: float = 0.5,
) -> None:
    """Run these teams' jobs, and what they enqueue, until none is queued or running."""
    team_ids = list(teams)
    give_up = time.monotonic() + timeout_s
    while True:
        job = None
        for lane in LANES:
            async with sessions.begin() as session:
                job = await queue.claim(session, lane, teams=team_ids)
            if job is not None:
                break
        if job is not None:
            await lanes.run_job(job, sessions, handlers)
        else:
            async with sessions() as session:
                waiting = (await session.execute(_WAITING, {"teams": team_ids})).all()
            if not waiting:
                return
            if time.monotonic() > give_up:
                kinds = ", ".join(f"{row.kind} ({row.n})" for row in waiting)
                raise SeedError(f"jobs still waiting after {timeout_s:g} s: {kinds}")
            await asyncio.sleep(idle_s)


def filling_transport(recorded: Transport, fresh: Transport) -> Transport:
    """Answer from the recordings; only a request with none goes to `fresh`."""

    async def send(body: dict[str, Any]) -> dict[str, Any]:
        try:
            return await recorded(body)
        except ReplayMissing:
            return await fresh(body)

    return send


def seed_transport(settings: Settings, mode: Mode) -> Transport:
    recorded = replay_transport(settings.llm_replay_dir)
    if mode == "replay":
        return recorded
    key = settings.llm_api_key.get_secret_value() if settings.llm_api_key is not None else ""
    if not key:
        raise SeedError("--mode record calls the model: set LLM_API_KEY in .env")
    live = live_transport(settings.llm_base_url, key, settings.llm_timeout_s)
    return filling_transport(recorded, recording_transport(live, settings.llm_replay_dir / "demo"))
