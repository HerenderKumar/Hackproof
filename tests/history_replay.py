"""Replaying a history fixture through the repo watch's own handlers (section 20.3).

The fixture's bare repository is built with every pushed head under `refs/pushes/`; `main` is
removed, then moved to each pushed head when its time comes, while a fake clock steps through
the kickoff and the push log and the kickoff and poll handlers run at each step.
"""

import json
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core import queue
from core.db import models
from core.providers.local import LocalGitProvider
from scripts.fixtures import HistoryFixture
from scripts.git_fixtures import PUSH_LOG, build_history_remote
from tests.factories import make_event, make_team
from tests.fakes import FakeClock
from worker.watch import make_kickoff_handler, make_poll_handler

KICKOFF = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)


async def replay(
    sessions: async_sessionmaker[AsyncSession], history: HistoryFixture, tmp_path: Path
) -> tuple[models.Event, models.Team, list[models.RepoObservation]]:
    remotes = tmp_path / "remotes"
    repo = build_history_remote(history, remotes, KICKOFF)
    pushes = [
        (datetime.fromisoformat(p["at"]), p["head"])
        for p in json.loads((repo / PUSH_LOG).read_text())
    ]
    _git(repo, "update-ref", "-d", "refs/heads/main")

    clock = FakeClock(KICKOFF)
    provider = LocalGitProvider(remotes, clock)
    info = await provider.describe(f"local://{history.slug}")
    team_id = uuid.uuid4()
    async with sessions.begin() as session:
        event = make_event(kickoff_at=KICKOFF, deadline_at=KICKOFF + timedelta(hours=24))
        session.add(event)
        await session.flush()
        session.add(
            make_team(
                event.id,
                id=team_id,
                repo_url=info.url,
                repo_provider="local",
                repository_id=info.id,
                default_branch=info.default_branch,
                repo_created_at=info.created_at,
                registered_at=KICKOFF - timedelta(days=1),
            )
        )

    kickoff = make_kickoff_handler(sessions, provider, clock)
    poll = make_poll_handler(sessions, provider, clock)
    for at in sorted({KICKOFF, *(when for when, _ in pushes)}):
        pushed = [head for when, head in pushes if when <= at]
        if pushed:
            _git(repo, "update-ref", "refs/heads/main", pushed[-1])
        clock.at = at
        for handler, kind in ((kickoff, "kickoff_snapshot"), (poll, "poll_repo")):
            outcome = await handler(queue.Job.model_construct(kind=kind, team_id=team_id))
            assert outcome is None, outcome

    async with sessions() as session:
        stored_event = await session.get(models.Event, event.id)
        team = await session.get(models.Team, team_id)
        rows = await session.scalars(
            select(models.RepoObservation)
            .where(models.RepoObservation.team_id == team_id)
            .order_by(models.RepoObservation.observed_at, models.RepoObservation.id)
        )
        assert stored_event is not None and team is not None
        return stored_event, team, list(rows)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "--git-dir", str(repo), *args], check=True, capture_output=True)
