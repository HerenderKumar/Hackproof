"""Queue core (section 11): idempotent enqueue, claim order, leases, attempts, dependencies."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from core import queue
from core.codemap import GRAPHIFY_TIMEOUT_S
from core.settings import Settings
from tests.helpers import Sessions, claim, enqueue, expire_lease, job_row, make_runnable_now

# --- enqueue -------------------------------------------------------------------------------


async def test_enqueue_twice_with_one_key_creates_one_job(sessions: Sessions) -> None:
    async with sessions.begin() as s:
        first, created_first = await queue.enqueue(s, "final_run", "final_run:abc")
    async with sessions.begin() as s:
        second, created_second = await queue.enqueue(s, "final_run", "final_run:abc")

    assert first == second
    assert (created_first, created_second) == (True, False)
    async with sessions() as s:
        count = (await s.execute(text("SELECT count(*) FROM jobs"))).scalar_one()
    assert count == 1


async def test_enqueue_takes_lane_and_priority_from_the_kind(sessions: Sessions) -> None:
    poll = await job_row(sessions, await enqueue(sessions, "poll_repo"))
    final = await job_row(sessions, await enqueue(sessions, "final_run"))

    assert (poll.lane, poll.priority, poll.status) == ("watch", 80, "queued")
    assert (final.lane, final.priority) == ("heavy", 100)


async def test_enqueue_stores_payload_and_links(sessions: Sessions) -> None:
    job_id = await enqueue(sessions, "draft_specs", payload={"claims_version": 2})
    row = await job_row(sessions, job_id)
    assert row.payload == {"claims_version": 2}


def test_kinds_match_the_table_in_section_11() -> None:
    table = {name: (k.lane, k.priority, k.lease_s) for name, k in queue.KINDS.items()}
    assert table == {
        "kickoff_snapshot": ("watch", 90, 120),
        "poll_repo": ("watch", 80, 60),
        "prepare_snapshot": ("heavy", 60, 300),
        "draft_specs": ("heavy", 50, 300),
        "self_check_run": ("heavy", 10, 1200),
        "final_run": ("heavy", 100, 1200),
        "audit": ("heavy", 100, 600),  # the brief's 300 is shorter than an audit (D-053)
        "brief": ("heavy", 100, 180),
    }


def test_the_audit_lease_outlasts_the_longest_audit() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    # The time budget is checked after a step: the step under way and the final request each
    # take up to LLM_TIMEOUT_S; Graphify runs after the audit.
    longest = settings.audit_timeout_s + 2 * settings.llm_timeout_s + GRAPHIFY_TIMEOUT_S
    assert queue.KINDS["audit"].lease_s > longest


def test_poll_key_is_bucketed_by_the_poll_interval() -> None:
    team = uuid.UUID(int=7)
    base = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
    same_bucket = queue.poll_key(team, base + timedelta(seconds=299), 300)
    assert queue.poll_key(team, base, 300) == same_bucket
    assert queue.poll_key(team, base + timedelta(seconds=300), 300) != same_bucket
    assert same_bucket == f"poll:{team}:{int(base.timestamp()) // 300}"


# --- claim ---------------------------------------------------------------------------------


async def test_claim_takes_the_highest_priority_first(sessions: Sessions) -> None:
    low = await enqueue(sessions, "self_check_run")
    mid = await enqueue(sessions, "draft_specs")
    high = await enqueue(sessions, "final_run")

    claimed = [await claim(sessions, "heavy") for _ in range(3)]

    assert [j.id for j in claimed if j] == [high, mid, low]


async def test_equal_priority_is_claimed_oldest_first(sessions: Sessions) -> None:
    first = await enqueue(sessions, "final_run")
    second = await enqueue(sessions, "final_run")

    a = await claim(sessions, "heavy")
    b = await claim(sessions, "heavy")

    assert a is not None and b is not None
    assert [a.id, b.id] == [first, second]


async def test_claim_only_sees_its_own_lane(sessions: Sessions) -> None:
    await enqueue(sessions, "final_run")
    assert await claim(sessions, "watch") is None


async def test_claim_marks_the_job_running_with_a_fresh_attempt_and_a_lease(
    sessions: Sessions,
) -> None:
    job_id = await enqueue(sessions, "poll_repo")

    job = await claim(sessions, "watch")

    assert job is not None and job.id == job_id
    row = await job_row(sessions, job_id)
    assert row.status == "running"
    assert row.attempts == 1
    assert row.attempt_id is not None and row.attempt_id == job.attempt_id
    async with sessions() as s:
        lease_left = (
            await s.execute(
                text("SELECT extract(epoch FROM lease_until - now()) FROM jobs WHERE id = :id"),
                {"id": job_id},
            )
        ).scalar_one()
    assert 55 <= float(lease_left) <= 60  # poll_repo's lease is 60 seconds


async def test_a_leased_job_is_not_claimed_twice(sessions: Sessions) -> None:
    await enqueue(sessions, "final_run")
    assert await claim(sessions, "heavy") is not None
    assert await claim(sessions, "heavy") is None


async def test_concurrent_claims_never_return_the_same_job(sessions: Sessions) -> None:
    for _ in range(4):
        await enqueue(sessions, "poll_repo")

    claimed = await asyncio.gather(*(claim(sessions, "watch") for _ in range(4)))

    ids = [j.id for j in claimed if j is not None]
    assert len(ids) == len(set(ids)) == 4


async def test_a_job_is_not_claimed_before_run_after(sessions: Sessions) -> None:
    await enqueue(sessions, "final_run", run_after=datetime.now(UTC) + timedelta(minutes=5))
    assert await claim(sessions, "heavy") is None


# --- leases and attempts -------------------------------------------------------------------


async def test_a_job_is_reclaimed_with_a_new_attempt_after_its_lease_expires(
    sessions: Sessions,
) -> None:
    job_id = await enqueue(sessions, "final_run")
    first = await claim(sessions, "heavy")
    assert first is not None

    await expire_lease(sessions, job_id)
    second = await claim(sessions, "heavy")

    assert second is not None and second.id == job_id
    assert second.attempt_id != first.attempt_id
    assert second.attempts == 2


async def test_completing_with_a_stale_attempt_is_rejected(sessions: Sessions) -> None:
    job_id = await enqueue(sessions, "final_run")
    stale = await claim(sessions, "heavy")
    await expire_lease(sessions, job_id)
    current = await claim(sessions, "heavy")
    assert stale is not None and current is not None

    async with sessions.begin() as s:
        accepted = await queue.complete(s, job_id, stale.attempt_id)

    assert accepted is False
    row = await job_row(sessions, job_id)
    assert (row.status, row.attempt_id, row.finished_at) == ("running", current.attempt_id, None)


async def test_completing_with_the_current_attempt_marks_the_job_done(sessions: Sessions) -> None:
    job_id = await enqueue(sessions, "final_run")
    job = await claim(sessions, "heavy")
    assert job is not None

    async with sessions.begin() as s:
        accepted = await queue.complete(s, job_id, job.attempt_id)

    row = await job_row(sessions, job_id)
    assert accepted is True
    assert row.status == "done" and row.finished_at is not None


async def test_completion_can_record_a_degraded_outcome(sessions: Sessions) -> None:
    job_id = await enqueue(sessions, "audit")
    job = await claim(sessions, "heavy")
    assert job is not None

    async with sessions.begin() as s:
        await queue.complete(
            s, job_id, job.attempt_id, error_class="budget", error_detail="token budget hit"
        )

    row = await job_row(sessions, job_id)
    assert (row.status, row.error_class, row.error_detail) == ("done", "budget", "token budget hit")


async def test_a_reclaimed_job_past_max_attempts_is_failed_instead_of_run(
    sessions: Sessions,
) -> None:
    job_id = await enqueue(sessions, "final_run", max_attempts=1)
    assert await claim(sessions, "heavy") is not None
    await expire_lease(sessions, job_id)

    assert await claim(sessions, "heavy") is None

    row = await job_row(sessions, job_id)
    assert (row.status, row.error_class) == ("failed", "infra")
    assert row.finished_at is not None


# --- retries -------------------------------------------------------------------------------


async def seconds_until_run_after(sessions: Sessions, job_id: uuid.UUID) -> float:
    async with sessions() as s:
        value = (
            await s.execute(
                text("SELECT extract(epoch FROM run_after - now()) FROM jobs WHERE id = :id"),
                {"id": job_id},
            )
        ).scalar_one()
    return float(value)


async def test_an_infrastructure_error_requeues_with_growing_backoff(sessions: Sessions) -> None:
    job_id = await enqueue(sessions, "final_run")

    first = await claim(sessions, "heavy")
    assert first is not None
    async with sessions.begin() as s:
        outcome = await queue.retry_or_fail(s, first, "infra", "docker daemon error")
    assert outcome == "retried"
    row = await job_row(sessions, job_id)
    assert (row.status, row.error_class, row.error_detail) == (
        "queued", "infra", "docker daemon error",
    )  # fmt: skip
    assert 25 <= await seconds_until_run_after(sessions, job_id) <= 30

    await make_runnable_now(sessions, job_id)
    second = await claim(sessions, "heavy")
    assert second is not None
    async with sessions.begin() as s:
        await queue.retry_or_fail(s, second, "infra", "again")
    assert 115 <= await seconds_until_run_after(sessions, job_id) <= 120


async def test_an_infrastructure_error_fails_the_job_when_no_attempts_remain(
    sessions: Sessions,
) -> None:
    job_id = await enqueue(sessions, "final_run", max_attempts=1)
    job = await claim(sessions, "heavy")
    assert job is not None

    async with sessions.begin() as s:
        outcome = await queue.retry_or_fail(s, job, "infra", "runner crashed")

    assert outcome == "failed"
    row = await job_row(sessions, job_id)
    assert (row.status, row.error_class, row.error_detail) == ("failed", "infra", "runner crashed")
    assert row.finished_at is not None


async def test_an_error_that_is_not_infrastructure_fails_without_a_retry(
    sessions: Sessions,
) -> None:
    job_id = await enqueue(sessions, "draft_specs")
    job = await claim(sessions, "heavy")
    assert job is not None and job.attempts < job.max_attempts

    async with sessions.begin() as s:
        outcome = await queue.retry_or_fail(s, job, "validation", "unusable output")

    assert outcome == "failed"
    row = await job_row(sessions, job_id)
    assert (row.status, row.error_class) == ("failed", "validation")


async def test_a_stale_attempt_cannot_requeue_the_job(sessions: Sessions) -> None:
    job_id = await enqueue(sessions, "final_run")
    stale = await claim(sessions, "heavy")
    await expire_lease(sessions, job_id)
    current = await claim(sessions, "heavy")
    assert stale is not None and current is not None

    async with sessions.begin() as s:
        outcome = await queue.retry_or_fail(s, stale, "infra", "late error")

    assert outcome == "stale"
    row = await job_row(sessions, job_id)
    assert (row.status, row.attempt_id) == ("running", current.attempt_id)


# --- dependencies --------------------------------------------------------------------------


async def test_a_dependent_job_waits_until_its_dependency_is_done(sessions: Sessions) -> None:
    # `self_check_run` has the lowest priority, so only the dependency holds back `brief`.
    first = await enqueue(sessions, "self_check_run")
    dependent = await enqueue(sessions, "brief", depends_on=first)

    job = await claim(sessions, "heavy")
    assert job is not None and job.id == first
    assert await claim(sessions, "heavy") is None

    async with sessions.begin() as s:
        await queue.complete(s, first, job.attempt_id)
    after = await claim(sessions, "heavy")
    assert after is not None and after.id == dependent


async def test_a_dependent_job_runs_after_its_dependency_fails(sessions: Sessions) -> None:
    first = await enqueue(sessions, "audit", max_attempts=1)
    dependent = await enqueue(sessions, "brief", depends_on=first)
    job = await claim(sessions, "heavy")
    assert job is not None and job.id == first

    async with sessions.begin() as s:
        await queue.retry_or_fail(s, job, "infra", "claude unavailable")

    after = await claim(sessions, "heavy")
    assert after is not None and after.id == dependent


def test_one_draft_job_per_team_and_claims_version() -> None:
    team = uuid.UUID("11111111-1111-1111-1111-111111111111")
    assert queue.draft_key(team, 3) == f"draft:{team}:3"


def test_one_self_check_job_per_snapshot_and_request() -> None:
    snapshot = uuid.UUID("22222222-2222-2222-2222-222222222222")
    assert queue.self_check_key(snapshot, "request-0001") == f"self_check:{snapshot}:request-0001"


def test_one_final_run_per_submission() -> None:
    submission = uuid.UUID("33333333-3333-3333-3333-333333333333")
    assert queue.final_run_key(submission) == f"final_run:{submission}"
