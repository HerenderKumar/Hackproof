"""Deleting device code after the event's retention (M9, Task 18)."""

import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from core.db import models
from core.settings import Settings
from tests.baseline_flow import indexed_baseline, submitted
from tests.factories import make_event, make_team
from tests.helpers import Sessions, claimed
from tests.priorwork_fakes import block
from worker import lanes
from worker.prior_work_job import make_prior_work_handler
from worker.purge_job import make_purge_handler

KEY_HEX = "ef" * 32
KEY = bytes.fromhex(KEY_HEX)
OLD = block(1)


@pytest.fixture
def pool() -> Iterator[ThreadPoolExecutor]:
    with ThreadPoolExecutor(1) as executor:
        yield executor


def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        host_artifact_dir=tmp_path, baseline_key=KEY_HEX, llm_mode="replay", repo_provider="local",
    )  # fmt: skip


async def event_with_report(
    sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor
) -> tuple[uuid.UUID, models.Baseline]:
    """An event whose team has a baseline, a submission and a ready report with a match."""
    async with sessions.begin() as s:
        event = make_event()
        s.add(event)
        await s.flush()
        team = make_team(event.id)
        s.add(team)
    baseline = await indexed_baseline(sessions, tmp_path, KEY, team.id, {"o.js": OLD.encode()})
    submission = await submitted(sessions, tmp_path, team.id, {"a.js": (OLD + block(2)).encode()})
    job = await claimed(sessions, "prior_work", submission_id=submission.id, team_id=team.id)
    handler = make_prior_work_handler(sessions, pool, settings(tmp_path))
    await lanes.run_job(job, sessions, {"prior_work": handler})
    async with sessions.begin() as s:
        s.add(models.MatchExplanation(submission_id=submission.id, text="our own old code"))
    return event.id, baseline


async def purge(sessions: Sessions, tmp_path: Path, event_id: uuid.UUID) -> Any:
    job = await claimed(sessions, "purge_baselines", payload={"event_id": str(event_id)})
    return await make_purge_handler(sessions, settings(tmp_path))(job)


async def rows(sessions: Sessions, model: Any) -> list[Any]:
    async with sessions() as s:
        return list((await s.scalars(select(model))).all())


async def test_a_purge_deletes_parts_indexes_and_excerpts(
    sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor
) -> None:
    event_id, baseline = await event_with_report(sessions, tmp_path, pool)
    folder = tmp_path / "baselines" / str(baseline.id)
    assert (folder / "index.sqlite").is_file()

    assert await purge(sessions, tmp_path, event_id) is None

    assert not folder.exists()
    [stored] = await rows(sessions, models.Baseline)
    assert stored.status == "purged"
    [match] = await rows(sessions, models.PriorWorkMatch)
    assert (match.final_excerpt, match.old_excerpt) == (None, None)
    [report] = await rows(sessions, models.PriorWorkReport)
    assert report.purged_at is not None


async def test_a_purge_keeps_numbers_paths_and_explanations(
    sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor
) -> None:
    event_id, _ = await event_with_report(sessions, tmp_path, pool)
    [before] = await rows(sessions, models.PriorWorkReport)
    [match_before] = await rows(sessions, models.PriorWorkMatch)

    await purge(sessions, tmp_path, event_id)

    [after] = await rows(sessions, models.PriorWorkReport)
    [match] = await rows(sessions, models.PriorWorkMatch)
    assert (after.totals, after.files) == (before.totals, before.files)
    assert (match.final_path, match.old_path, match.tokens, match.match_key) == (
        match_before.final_path, match_before.old_path, match_before.tokens, match_before.match_key,
    )  # fmt: skip
    [explanation] = await rows(sessions, models.MatchExplanation)
    assert explanation.text == "our own old code"


async def test_a_purge_leaves_other_events_alone(
    sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor
) -> None:
    event_id, gone = await event_with_report(sessions, tmp_path, pool)
    _, kept = await event_with_report(sessions, tmp_path, pool)

    await purge(sessions, tmp_path, event_id)

    statuses = {b.id: b.status for b in await rows(sessions, models.Baseline)}
    assert statuses == {gone.id: "purged", kept.id: "indexed"}
    assert (tmp_path / "baselines" / str(kept.id) / "index.sqlite").is_file()
    excerpts = [m.final_excerpt is not None for m in await rows(sessions, models.PriorWorkMatch)]
    assert sorted(excerpts) == [False, True]


async def test_a_purge_run_twice_changes_nothing(
    sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor
) -> None:
    event_id, _ = await event_with_report(sessions, tmp_path, pool)
    await purge(sessions, tmp_path, event_id)
    [first] = await rows(sessions, models.PriorWorkReport)

    assert await purge(sessions, tmp_path, event_id) is None

    [again] = await rows(sessions, models.PriorWorkReport)
    assert again.purged_at == first.purged_at
