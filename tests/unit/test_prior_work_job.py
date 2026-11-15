"""The `prior_work` job (M9, Task 16): a final submission against the team's baselines."""

import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from core import queue
from core.baselines.crypto import decrypt
from core.db import models
from core.priorwork.model import MatchSettings
from core.priorwork.run import starter_runs
from core.settings import Settings
from tests.baseline_flow import indexed_baseline, submitted
from tests.factories import make_event, make_team
from tests.helpers import Sessions, claimed
from tests.priorwork_fakes import FAKE_MIN_SCORE, REWRITE_NEW, REWRITE_OLD, block, fake_embedder
from worker.lanes import NotReady
from worker.prior_work_job import embedder_factory, make_prior_work_handler

KEY_HEX = "cd" * 32
KEY = bytes.fromhex(KEY_HEX)
OLD, NEW = block(1), block(2)


@pytest.fixture
def pool() -> Iterator[ThreadPoolExecutor]:
    with ThreadPoolExecutor(1) as executor:
        yield executor


def settings(tmp_path: Path, **changes: Any) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        host_artifact_dir=tmp_path,
        baseline_key=KEY_HEX,
        llm_mode="replay",
        repo_provider="local",
        **changes,
    )


async def teams(sessions: Sessions, count: int = 1) -> tuple[uuid.UUID, list[uuid.UUID]]:
    async with sessions.begin() as s:
        event = make_event()
        s.add(event)
        await s.flush()
        made = [make_team(event.id, name=f"Team {n}") for n in range(count)]
        s.add_all(made)
    return event.id, [t.id for t in made]


async def run(
    sessions: Sessions,
    tmp_path: Path,
    pool: ThreadPoolExecutor,
    submission: Any,
    **changes: Any,
) -> tuple[Any, queue.Job]:
    job = await claimed(
        sessions, "prior_work", submission_id=submission.id, team_id=submission.team_id
    )
    outcome = await make_prior_work_handler(sessions, pool, settings(tmp_path, **changes))(job)
    return outcome, job


async def reports(sessions: Sessions) -> list[models.PriorWorkReport]:
    async with sessions() as s:
        query = select(models.PriorWorkReport).order_by(models.PriorWorkReport.created_at)
        return list((await s.scalars(query)).all())


async def matches(sessions: Sessions, report_id: uuid.UUID) -> list[models.PriorWorkMatch]:
    async with sessions() as s:
        query = (
            select(models.PriorWorkMatch)
            .where(models.PriorWorkMatch.report_id == report_id)
            .order_by(models.PriorWorkMatch.ordinal)
        )
        return list((await s.scalars(query)).all())


async def test_a_team_with_no_baseline_gets_a_no_baseline_report(
    sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor
) -> None:
    _, [team] = await teams(sessions)
    submission = await submitted(sessions, tmp_path, team, {"a.js": OLD.encode()})
    outcome, job = await run(sessions, tmp_path, pool, submission)
    [report] = await reports(sessions)
    assert outcome is None
    assert (report.status, report.totals, report.attempt_id) == (
        "no_baseline",
        None,
        job.attempt_id,
    )


async def test_the_job_waits_for_a_baseline_still_being_indexed(
    sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor
) -> None:
    _, [team] = await teams(sessions)
    await indexed_baseline(sessions, tmp_path, KEY, team, {"a.js": b"a"}, status="sealed")
    submission = await submitted(sessions, tmp_path, team, {"a.js": OLD.encode()})
    with pytest.raises(NotReady) as waiting:
        await run(sessions, tmp_path, pool, submission)
    assert waiting.value.delay_s == 30
    assert await reports(sessions) == []


async def test_a_report_holds_totals_settings_baselines_and_matches(
    sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor
) -> None:
    _, [team] = await teams(sessions)
    baseline = await indexed_baseline(sessions, tmp_path, KEY, team, {"old/lib.js": OLD.encode()})
    submission = await submitted(sessions, tmp_path, team, {"src/app.js": (OLD + NEW).encode()})

    await run(sessions, tmp_path, pool, submission)

    [report] = await reports(sessions)
    assert report.status == "ready" and report.baseline_ids == [str(baseline.id)]
    assert report.settings["k"] == 25 and report.settings["min_teams"] == 3
    assert ".js" in report.settings["extensions"]
    assert 0 < report.totals["percent"] < 100 and report.totals["late_matched"] == 0
    assert [f["path"] for f in report.files] == ["src/app.js"]
    [match] = await matches(sessions, report.id)
    assert (match.kind, match.final_path, match.old_path, match.late) == (
        "unchanged", "src/app.js", "old/lib.js", False,
    )  # fmt: skip
    assert match.baseline_id == baseline.id and match.device_label == "laptop"
    assert len(match.match_key) == 32 and match.tokens == report.totals["matched"]


async def test_a_late_baseline_is_reported_apart(
    sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor
) -> None:
    _, [team] = await teams(sessions)
    await indexed_baseline(sessions, tmp_path, KEY, team, {"old.js": OLD.encode()}, late=True)
    submission = await submitted(sessions, tmp_path, team, {"app.js": (OLD + NEW).encode()})
    await run(sessions, tmp_path, pool, submission)
    [report] = await reports(sessions)
    assert report.totals["matched"] == 0 and report.totals["percent"] == 0.0
    assert report.totals["late_matched"] > 0
    assert [m.late for m in await matches(sessions, report.id)] == [True]


async def test_excerpts_are_stored_encrypted(
    sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor
) -> None:
    _, [team] = await teams(sessions)
    marker = "const prior_work_marker_51 = 1;\n"
    await indexed_baseline(sessions, tmp_path, KEY, team, {"old.js": (marker + OLD).encode()})
    submission = await submitted(sessions, tmp_path, team, {"app.js": (marker + OLD).encode()})
    await run(sessions, tmp_path, pool, submission)

    [report] = await reports(sessions)
    [match] = await matches(sessions, report.id)
    assert match.kind == "identical_file"
    for column, side in ((match.final_excerpt, "final"), (match.old_excerpt, "old")):
        assert column is not None and b"prior_work_marker_51" not in column
        text = decrypt(KEY, column, f"match:{match.id}:{side}".encode()).decode()
        assert text.startswith(marker.strip())
    assert (match.final_excerpt_line, match.old_excerpt_line) == (1, 1)


async def test_a_team_whose_baselines_all_failed_gets_a_failed_report(
    sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor
) -> None:
    _, [team] = await teams(sessions)
    await indexed_baseline(sessions, tmp_path, KEY, team, {"a.js": b"a"}, status="failed")
    submission = await submitted(sessions, tmp_path, team, {"a.js": OLD.encode()})
    await run(sessions, tmp_path, pool, submission)
    [report] = await reports(sessions)
    assert (report.status, report.detail) == ("failed", "the team's baselines could not be indexed")


async def test_another_teams_code_only_decides_what_is_common(
    sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor
) -> None:
    _, [team, rival_one, rival_two] = await teams(sessions, 3)
    shared, theirs = block(3), block(4)
    await indexed_baseline(sessions, tmp_path, KEY, team, {"s.js": shared.encode()})
    for rival in (rival_one, rival_two):
        files = {"s.js": shared.encode(), "t.js": theirs.encode()}
        await indexed_baseline(sessions, tmp_path, KEY, rival, files)
    final = {"app.js": (shared + NEW).encode(), "copied.js": (theirs + block(5)).encode()}
    submission = await submitted(sessions, tmp_path, team, final)

    await run(sessions, tmp_path, pool, submission)

    [report] = await reports(sessions)
    assert await matches(sessions, report.id) == []  # theirs is not the team's; shared is common
    assert report.totals["excluded_common"] > 0 and report.totals["matched"] == 0


async def test_an_approved_starter_is_left_out(
    sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor
) -> None:
    event_id, [team] = await teams(sessions)
    starter = block(6)
    async with sessions.begin() as s:
        s.add(
            models.Starter(
                event_id=event_id, name="kit", archive_sha256="e" * 64, files=1, settings={},
                runs=starter_runs([("index.js", starter.encode())], MatchSettings()),
            )
        )  # fmt: skip
    await indexed_baseline(sessions, tmp_path, KEY, team, {"kit.js": starter.encode()})
    submission = await submitted(sessions, tmp_path, team, {"app.js": (starter + NEW).encode()})
    await run(sessions, tmp_path, pool, submission)
    [report] = await reports(sessions)
    assert report.totals["excluded_starter"] > 0 and report.totals["matched"] == 0
    assert await matches(sessions, report.id) == []


async def test_a_rerun_writes_a_new_report_with_the_same_match_keys(
    sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor
) -> None:
    _, [team] = await teams(sessions)
    await indexed_baseline(
        sessions, tmp_path, KEY, team, {"a.js": OLD.encode(), "b.js": block(7).encode()}
    )
    final = {"x.js": (OLD + NEW).encode(), "y.js": (block(7) + block(8)).encode()}
    submission = await submitted(sessions, tmp_path, team, final)
    handler = make_prior_work_handler(sessions, pool, settings(tmp_path))
    job = await claimed(sessions, "prior_work", submission_id=submission.id, team_id=team)

    await handler(job)
    await handler(job)  # the same attempt again stores nothing more
    await handler(job.model_copy(update={"attempt_id": uuid.uuid4()}))

    first, second = await reports(sessions)
    keys = [[m.match_key for m in await matches(sessions, r.id)] for r in (first, second)]
    assert len(keys[0]) == 2 and keys[0] == keys[1]
    assert first.totals == second.totals


def test_the_model_is_asked_for_only_when_embeddings_are_on(tmp_path: Path) -> None:
    from worker.embedder import fast_embedder

    assert embedder_factory(settings(tmp_path)) is None
    assert embedder_factory(settings(tmp_path, embeddings="on")) is fast_embedder


async def test_a_possibly_related_pair_is_stored_and_never_counted(
    sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("worker.prior_work_job.embedder_factory", lambda _: fake_embedder)
    _, [team] = await teams(sessions)
    await indexed_baseline(sessions, tmp_path, KEY, team, {"old/geo.js": REWRITE_OLD.encode()})
    submission = await submitted(sessions, tmp_path, team, {"src/geo.js": REWRITE_NEW.encode()})

    await run(
        sessions, tmp_path, pool, submission, embeddings="on", related_min_score=FAKE_MIN_SCORE
    )

    [report] = await reports(sessions)
    assert report.totals["percent"] == 0.0 and report.totals["matched"] == 0
    assert report.settings["related_min_score"] == FAKE_MIN_SCORE
    [match] = await matches(sessions, report.id)
    assert (match.kind, match.tokens, match.old_path) == ("possibly_related", 0, "old/geo.js")
    assert match.score is not None and match.score >= FAKE_MIN_SCORE
