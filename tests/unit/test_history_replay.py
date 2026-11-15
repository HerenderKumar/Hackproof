"""H1 and H2 replayed through the repo watch's handlers yield their expected signals (20.3)."""

import json
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.signals import push_log, signals
from scripts.fixtures import HistoryFixture, load_history_fixtures
from scripts.git_fixtures import PUSH_LOG
from tests.history_replay import replay

Sessions = async_sessionmaker[AsyncSession]


@pytest.mark.parametrize("history", load_history_fixtures(), ids=lambda h: h.id)
async def test_a_history_fixture_yields_its_expected_signals(
    sessions: Sessions, tmp_path: Path, history: HistoryFixture
) -> None:
    event, team, rows = await replay(sessions, history, tmp_path)

    found = signals(
        kickoff_at=event.kickoff_at,
        bulk_push_lines=event.bulk_push_lines,
        bulk_push_window_minutes=event.bulk_push_window_minutes,
        repo_created_at=team.repo_created_at,
        observations=rows,
    )

    assert [s.name for s in found] == history.expected_signals


async def test_h2_keeps_all_four_heads(sessions: Sessions, tmp_path: Path) -> None:
    h2 = next(h for h in load_history_fixtures() if h.id == "H2")

    _event, _team, rows = await replay(sessions, h2, tmp_path)

    pushed = json.loads((tmp_path / "remotes" / f"{h2.slug}.git" / PUSH_LOG).read_text())
    assert [e.head_sha for e in push_log(rows)] == [None, *(p["head"] for p in pushed)]
    assert [r.descends_from_previous for r in rows] == [None, True, True, True, False]
