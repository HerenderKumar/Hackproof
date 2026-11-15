"""The worker process: one process, two lanes, a scheduler and a heartbeat (section 11)."""

import asyncio
import signal
from collections.abc import Mapping

from sqlalchemy import text

from core.clock import Clock, SystemClock
from core.db.engine import Sessions, make_engine, make_sessions
from core.llm import LLMClient, make_llm_client
from core.logging import configure_logging, get_logger
from core.providers import make_provider
from core.settings import Settings, get_settings
from worker import heartbeat, lanes, scheduler
from worker.audit_job import make_audit_handler
from worker.brief_job import make_brief_handler
from worker.cleanup import startup_cleanup
from worker.draft_job import make_draft_handler
from worker.loops import every, wait_until
from worker.run_job import RUN_KINDS, make_run_handler
from worker.runtime import ContainerRuntime, DockerRuntime
from worker.snapshot import make_prepare_snapshot
from worker.watch import make_kickoff_handler, make_poll_handler

SCHEMA_POLL_S = 2.0


def build_handlers(
    settings: Settings,
    sessions: Sessions,
    clock: Clock,
    runtime: ContainerRuntime,
    *,
    llm: LLMClient | None = None,
) -> dict[str, lanes.Handler]:
    """Job handlers by kind. Each pipeline milestone adds its own. `llm` replaces the settings'
    model client (the demo's seed passes one with its own transport)."""
    provider = make_provider(settings, clock)
    run = make_run_handler(sessions, runtime, settings, clock)
    llm = llm or make_llm_client(settings, sessions)
    return {
        "prepare_snapshot": make_prepare_snapshot(
            sessions, provider, settings.artifact_dir / "archives"
        ),
        "draft_specs": make_draft_handler(sessions, llm, settings),
        **dict.fromkeys(RUN_KINDS, run),
        "audit": make_audit_handler(sessions, llm, settings),
        "brief": make_brief_handler(sessions, llm, settings),
        "kickoff_snapshot": make_kickoff_handler(sessions, provider, clock),
        "poll_repo": make_poll_handler(sessions, provider, clock),
    }


async def run(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    settings.require_for_worker()
    configure_logging(service="worker", level=settings.log_level)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    engine = make_engine(settings.database_url)
    sessions, clock, runtime = make_sessions(engine), SystemClock(), DockerRuntime()
    handlers = build_handlers(settings, sessions, clock, runtime)
    try:
        await serve(settings, sessions, runtime, clock, handlers, stop)
    finally:
        await engine.dispose()


async def schema_ready(sessions: Sessions) -> bool:
    """True once the migrations have created the job table."""
    async with sessions() as session:
        found = await session.execute(text("SELECT to_regclass('public.jobs') IS NOT NULL"))
        return bool(found.scalar_one())


async def serve(
    settings: Settings,
    sessions: Sessions,
    runtime: ContainerRuntime,
    clock: Clock,
    handlers: Mapping[str, lanes.Handler],
    stop: asyncio.Event,
    *,
    idle_s: float = 1.0,
) -> None:
    """Run the worker until `stop` is set. In-flight jobs finish first."""
    log = get_logger()

    # `make up` starts the worker before `make migrate` has run. Wait for the schema instead
    # of failing every claim, and say why once.
    announced: set[str] = set()

    async def database_is_migrated() -> bool:
        try:
            migrated, waiting_for = await schema_ready(sessions), "waiting_for_migrations"
        except Exception:
            migrated, waiting_for = False, "waiting_for_database"
        if not migrated and waiting_for not in announced:
            announced.add(waiting_for)
            log.warning(waiting_for)
        return migrated

    if not await wait_until(database_is_migrated, stop, poll_s=SCHEMA_POLL_S):
        return

    artifact_dir = settings.artifact_dir
    work_root = artifact_dir / "work"
    await asyncio.to_thread(work_root.mkdir, parents=True, exist_ok=True)

    docker = heartbeat.DockerState()
    await heartbeat.beat(runtime, artifact_dir, clock, docker)
    try:
        await startup_cleanup(sessions, runtime, work_root, clock.now())
    except Exception:
        log.exception("startup_cleanup_failed")

    log.info(
        "worker_started",
        llm_mode=settings.llm_mode,
        repo_provider=settings.repo_provider,
        watch_concurrency=settings.watch_concurrency,
        heavy_concurrency=settings.heavy_concurrency,
        docker_reachable=docker.reachable,
    )
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(
            every(
                heartbeat.INTERVAL_S,
                lambda: heartbeat.beat(runtime, artifact_dir, clock, docker),
                stop,
                name="heartbeat",
            )
        )
        tasks.create_task(
            every(
                scheduler.INTERVAL_S,
                lambda: scheduler.run_once(sessions, clock),
                stop,
                name="scheduler",
            )
        )
        for _ in range(settings.watch_concurrency):
            tasks.create_task(lanes.consume("watch", sessions, handlers, stop, idle_s=idle_s))
        for _ in range(settings.heavy_concurrency):
            # Builds and runs need Docker. While it is down, heavy jobs stay queued.
            tasks.create_task(
                lanes.consume(
                    "heavy", sessions, handlers, stop, ready=lambda: docker.reachable, idle_s=idle_s
                )
            )
    log.info("worker_stopped")
