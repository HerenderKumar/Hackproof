"""The worker's job loop (section 11): dispatch, completion, retry by error class, lanes."""

import asyncio
import json
from typing import Any

import pytest
import structlog

from core import queue
from core.errors import InfraError
from core.llm import ReplayMissing
from core.logging import configure_logging
from tests.helpers import Sessions, claim, claimed, enqueue, expire_lease, job_row
from worker import lanes

# --- one job -------------------------------------------------------------------------------


async def test_a_job_whose_handler_returns_is_marked_done(sessions: Sessions) -> None:
    job = await claimed(sessions, "poll_repo")
    seen: list[queue.Job] = []

    async def handler(j: queue.Job) -> None:
        seen.append(j)

    await lanes.run_job(job, sessions, {"poll_repo": handler})

    row = await job_row(sessions, job.id)
    assert seen == [job]
    assert (row.status, row.error_class) == ("done", None)


async def test_an_infrastructure_error_puts_the_job_back_in_the_queue(sessions: Sessions) -> None:
    job = await claimed(sessions, "poll_repo")

    async def handler(_: queue.Job) -> None:
        raise InfraError("github returned 502")

    await lanes.run_job(job, sessions, {"poll_repo": handler})

    row = await job_row(sessions, job.id)
    assert (row.status, row.error_class, row.error_detail) == (
        "queued", "infra", "github returned 502",
    )  # fmt: skip


async def test_an_unexpected_exception_is_retried_as_an_infrastructure_error(
    sessions: Sessions,
) -> None:
    job = await claimed(sessions, "poll_repo")

    async def handler(_: queue.Job) -> None:
        raise ValueError("boom")

    await lanes.run_job(job, sessions, {"poll_repo": handler})

    row = await job_row(sessions, job.id)
    assert (row.status, row.error_class) == ("queued", "infra")
    assert row.error_detail == "ValueError: boom"


async def test_a_missing_recording_finishes_the_job_without_retrying(sessions: Sessions) -> None:
    # Replay mode cannot find a reply on a later attempt either: the job ends at once, and the
    # brief or audit page renders without the model's part.
    job = await claimed(sessions, "brief")

    async def handler(_: queue.Job) -> None:
        raise ReplayMissing("brief: no recorded reply abc")

    await lanes.run_job(job, sessions, {"brief": handler})

    row = await job_row(sessions, job.id)
    assert (row.status, row.error_class) == ("done", "validation")
    assert row.error_detail == "ReplayMissing: brief: no recorded reply abc"


async def test_a_missing_recording_for_a_draft_is_retried(sessions: Sessions) -> None:
    # A draft that ends with no test would read as "browser test later" on the team's page.
    job = await claimed(sessions, "draft_specs")

    async def handler(_: queue.Job) -> None:
        raise ReplayMissing("draft: no recorded reply abc")

    await lanes.run_job(job, sessions, {"draft_specs": handler})

    row = await job_row(sessions, job.id)
    assert (row.status, row.error_class) == ("queued", "infra")


async def test_a_handler_can_finish_with_a_degraded_outcome(sessions: Sessions) -> None:
    job = await claimed(sessions, "audit")

    async def handler(_: queue.Job) -> lanes.Outcome:
        return lanes.Outcome(error_class="budget", error_detail="token budget hit")

    await lanes.run_job(job, sessions, {"audit": handler})

    row = await job_row(sessions, job.id)
    assert (row.status, row.error_class, row.error_detail) == ("done", "budget", "token budget hit")


async def test_a_job_with_no_handler_is_retried_as_an_infrastructure_error(
    sessions: Sessions,
) -> None:
    job = await claimed(sessions, "brief")

    await lanes.run_job(job, sessions, {})

    row = await job_row(sessions, job.id)
    assert (row.status, row.error_class) == ("queued", "infra")
    assert row.error_detail == "no handler registered for brief"


async def test_work_finished_by_a_stale_attempt_is_discarded(sessions: Sessions) -> None:
    job = await claimed(sessions, "final_run")
    takeover: list[queue.Job | None] = []

    async def handler(j: queue.Job) -> None:
        # While this attempt is still working, its lease runs out and another worker claims it.
        await expire_lease(sessions, j.id)
        takeover.append(await claim(sessions, "heavy"))

    await lanes.run_job(job, sessions, {"final_run": handler})

    row = await job_row(sessions, job.id)
    assert takeover[0] is not None
    assert (row.status, row.attempt_id) == ("running", takeover[0].attempt_id)


async def test_a_handlers_log_lines_carry_the_job_context(
    sessions: Sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(service="worker", level="INFO")
    job = await claimed(sessions, "poll_repo")

    async def handler(_: queue.Job) -> None:
        structlog.get_logger().info("inside_handler")

    await lanes.run_job(job, sessions, {"poll_repo": handler})

    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    inside = next(line for line in lines if line["event"] == "inside_handler")
    assert (inside["job_id"], inside["attempt_id"]) == (str(job.id), str(job.attempt_id))
    done = next(line for line in lines if line["event"] == "job_done")
    assert done["kind"] == "poll_repo" and done["duration_ms"] >= 0
    assert "job_id" not in structlog.contextvars.get_contextvars()


# --- a lane --------------------------------------------------------------------------------


async def test_a_lane_runs_its_queued_jobs_in_order_until_stopped(sessions: Sessions) -> None:
    first = await enqueue(sessions, "poll_repo")
    second = await enqueue(sessions, "poll_repo")
    stop = asyncio.Event()
    seen: list[Any] = []

    async def handler(j: queue.Job) -> None:
        seen.append(j.id)
        if len(seen) == 2:
            stop.set()

    await asyncio.wait_for(
        lanes.consume("watch", sessions, {"poll_repo": handler}, stop, idle_s=0.01), timeout=5
    )

    assert seen == [first, second]
    assert [(await job_row(sessions, j)).status for j in (first, second)] == ["done", "done"]


async def test_a_lane_that_is_not_ready_leaves_jobs_queued(sessions: Sessions) -> None:
    job_id = await enqueue(sessions, "final_run")
    stop = asyncio.Event()

    async def handler(_: queue.Job) -> None:
        raise AssertionError("claimed while the lane was not ready")

    lane = asyncio.create_task(
        lanes.consume(
            "heavy", sessions, {"final_run": handler}, stop, ready=lambda: False, idle_s=0.01
        )
    )
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(lane, timeout=5)

    row = await job_row(sessions, job_id)
    assert (row.status, row.attempts) == ("queued", 0)


async def test_a_lane_survives_a_failed_claim(sessions: Sessions) -> None:
    job_id = await enqueue(sessions, "poll_repo")
    stop = asyncio.Event()

    class FlakyDatabase:
        """Refuses the first transaction, as a restarting database would."""

        calls = 0

        def begin(self) -> Any:
            FlakyDatabase.calls += 1
            if FlakyDatabase.calls == 1:
                raise ConnectionError("database is restarting")
            return sessions.begin()

    async def handler(_: queue.Job) -> None:
        stop.set()

    await asyncio.wait_for(
        lanes.consume(
            "watch",
            FlakyDatabase(),  # type: ignore[arg-type]
            {"poll_repo": handler},
            stop,
            idle_s=0.01,
            error_pause_s=0.01,
        ),
        timeout=5,
    )

    assert (await job_row(sessions, job_id)).status == "done"


async def test_a_lane_backs_off_after_an_error_instead_of_retrying_at_once() -> None:
    stop = asyncio.Event()

    class DeadDatabase:
        calls = 0

        def begin(self) -> Any:
            DeadDatabase.calls += 1
            raise ConnectionError("database is down")

    lane = asyncio.create_task(
        lanes.consume("watch", DeadDatabase(), {}, stop, idle_s=0.001, error_pause_s=30)  # type: ignore[arg-type]
    )
    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(lane, timeout=5)

    assert DeadDatabase.calls == 1
