"""The job loop (section 11): claim a job, run its handler, record how the attempt ended.

Handlers store their own results, each row carrying the job's `attempt_id`. The loop then
completes the job. If the attempt went stale in the meantime (its lease ran out and another
claim took over), the completion is refused and the rows it wrote are ignored by readers.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

import structlog

from core import queue
from core.db.engine import Sessions
from core.errors import InfraError
from core.logging import get_logger
from worker.loops import pause

log = get_logger()


@dataclass(frozen=True)
class Outcome:
    """How a handler finished. With an `error_class` the job is still done, but degraded:
    a budget hit with partial output, or a validation fallback (section 11.3)."""

    error_class: queue.ErrorClass | None = None
    error_detail: str | None = None


Handler = Callable[[queue.Job], Awaitable[Outcome | None]]


async def run_job(job: queue.Job, sessions: Sessions, handlers: Mapping[str, Handler]) -> None:
    context = {
        "job_id": str(job.id),
        "attempt_id": str(job.attempt_id),
        "kind": job.kind,
        "team_id": job.team_id and str(job.team_id),
        "submission_id": job.submission_id and str(job.submission_id),
    }
    known = {name: value for name, value in context.items() if value is not None}
    with structlog.contextvars.bound_contextvars(**known):
        started = time.monotonic()

        def elapsed_ms() -> int:
            return int((time.monotonic() - started) * 1000)

        try:
            handler = handlers.get(job.kind)
            if handler is None:
                raise InfraError(f"no handler registered for {job.kind}")
            outcome = await handler(job) or Outcome()
        except Exception as exc:
            # Anything a handler did not turn into a result is treated as infrastructure:
            # the job is retried with the same inputs, then failed (section 19).
            expected = isinstance(exc, InfraError)
            detail = str(exc) if expected else f"{type(exc).__name__}: {exc}"
            async with sessions.begin() as session:
                result = await queue.retry_or_fail(session, job, "infra", detail)
            log.warning(
                "job_error",
                outcome=result,
                error_class="infra",
                error_detail=detail,
                duration_ms=elapsed_ms(),
                exc_info=not expected,
            )
            return

        async with sessions.begin() as session:
            accepted = await queue.complete(
                session,
                job.id,
                job.attempt_id,
                error_class=outcome.error_class,
                error_detail=outcome.error_detail,
            )
        if accepted:
            log.info("job_done", duration_ms=elapsed_ms(), error_class=outcome.error_class)
        else:
            log.warning("job_stale_attempt_discarded", duration_ms=elapsed_ms())


async def consume(
    lane: queue.Lane,
    sessions: Sessions,
    handlers: Mapping[str, Handler],
    stop: asyncio.Event,
    *,
    ready: Callable[[], bool] = lambda: True,
    idle_s: float = 1.0,
    error_pause_s: float = 5.0,
) -> None:
    """One consumer of a lane. Runs jobs back to back and idles when there is nothing to do.

    While `ready()` is false the lane claims nothing, so its jobs stay queued. The heavy lane
    uses this to wait for Docker (section 19). After an error of its own (the database is
    down, say) the lane waits longer before it tries again, so an outage does not flood the log.
    """
    while not stop.is_set():
        try:
            job = None
            if ready():
                async with sessions.begin() as session:
                    job = await queue.claim(session, lane)
            if job is not None:
                await run_job(job, sessions, handlers)
                continue
        except Exception:
            log.exception("lane_error", lane=lane)
            await pause(stop, error_pause_s)
            continue
        await pause(stop, idle_s)
