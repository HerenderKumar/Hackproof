"""The worker wired together: cleanup, heartbeat, scheduler and both lanes (section 11)."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from core import queue, worker_health
from core.settings import Settings
from tests.factories import T0, make_event, make_team
from tests.fakes import FakeClock, FakeRuntime
from tests.helpers import Sessions, all_jobs, enqueue, job_row
from worker.main import build_handlers, schema_ready, serve

NOW = T0 + timedelta(hours=3)


def worker_settings(artifact_dir: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        host_artifact_dir=artifact_dir,
        llm_mode="replay",
        repo_provider="local",
        watch_concurrency=2,
        heavy_concurrency=1,
    )


async def test_the_worker_cleans_up_reports_health_schedules_and_runs_both_lanes(
    sessions: Sessions, tmp_path: Path
) -> None:
    async with sessions.begin() as s:
        event = make_event()
        s.add(event)
        await s.flush()
        s.add(make_team(event.id, registered_at=T0 - timedelta(days=1)))
    final_run = await enqueue(sessions, "final_run")
    runtime = FakeRuntime(containers={"left-over": str(uuid.uuid4())})
    stop = asyncio.Event()
    handled: set[str] = set()

    async def handler(job: queue.Job) -> None:
        handled.add(job.kind)
        if handled == {"kickoff_snapshot", "poll_repo", "final_run"}:
            stop.set()

    await asyncio.wait_for(
        serve(
            worker_settings(tmp_path),
            sessions,
            runtime,
            FakeClock(NOW),
            dict.fromkeys(("kickoff_snapshot", "poll_repo", "final_run"), handler),
            stop,
            idle_s=0.01,
        ),
        timeout=10,
    )

    assert runtime.containers == {}
    health = worker_health.read(tmp_path)
    assert health is not None and health.docker_ok is True
    assert (tmp_path / "work").is_dir()
    assert (await job_row(sessions, final_run)).status == "done"
    assert {job.kind: job.status for job in await all_jobs(sessions)} == {
        "kickoff_snapshot": "done",
        "poll_repo": "done",
        "final_run": "done",
    }


async def test_heavy_jobs_stay_queued_while_docker_is_unreachable(
    sessions: Sessions, tmp_path: Path
) -> None:
    heavy = await enqueue(sessions, "final_run")
    watch = await enqueue(sessions, "poll_repo")
    stop = asyncio.Event()

    async def poll(_: queue.Job) -> None:
        await asyncio.sleep(0.1)  # give the heavy lane time to (wrongly) claim
        stop.set()

    async def final_run(_: queue.Job) -> None:
        raise AssertionError("a heavy job ran without Docker")

    await asyncio.wait_for(
        serve(
            worker_settings(tmp_path),
            sessions,
            FakeRuntime(reachable=False),
            FakeClock(NOW),
            {"poll_repo": poll, "final_run": final_run},
            stop,
            idle_s=0.01,
        ),
        timeout=10,
    )

    health = worker_health.read(tmp_path)
    assert health is not None and health.docker_ok is False
    assert (await job_row(sessions, watch)).status == "done"
    row = await job_row(sessions, heavy)
    assert (row.status, row.attempts) == ("queued", 0)


# --- before the migrations have run (`make up` starts the worker before `make migrate`) -----


@pytest.fixture
async def unmigrated(migrated_database_url: str) -> AsyncIterator[Sessions]:
    """Sessions on the server's `postgres` database, which has none of our tables."""
    url = make_url(migrated_database_url).set(database="postgres")
    engine = create_async_engine(url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_the_schema_is_ready_on_a_migrated_database(sessions: Sessions) -> None:
    assert await schema_ready(sessions) is True


async def test_the_schema_is_not_ready_before_the_migrations_run(unmigrated: Sessions) -> None:
    assert await schema_ready(unmigrated) is False


async def test_the_worker_waits_quietly_until_the_schema_exists(
    unmigrated: Sessions, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime = FakeRuntime(containers={"left-over": str(uuid.uuid4())})
    stop = asyncio.Event()
    worker = asyncio.create_task(
        serve(worker_settings(tmp_path), unmigrated, runtime, FakeClock(NOW), {}, stop, idle_s=0.01)
    )
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(worker, timeout=10)

    logged = capsys.readouterr().out
    assert "waiting_for_migrations" in logged
    assert "lane_error" not in logged and "Traceback" not in logged
    assert runtime.containers != {}  # nothing is cleaned up before the job table can be read


def test_the_worker_registers_a_handler_for_each_built_pipeline_step(tmp_path: Path) -> None:
    handlers = build_handlers(
        worker_settings(tmp_path), async_sessionmaker(), FakeClock(NOW), FakeRuntime()
    )

    assert set(handlers) == {"prepare_snapshot", "final_run", "self_check_run"}
