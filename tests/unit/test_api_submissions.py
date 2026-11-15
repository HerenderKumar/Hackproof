"""The final submission: immutable, idempotent, judged by the database's clock (M7, part 6)."""

import asyncio
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text

from core import queue
from core.db import models
from core.hashing import hash_json
from core.receipts import receipt_fields, receipt_hash
from tests.api_helpers import Api
from tests.fakes import FakeClock, FakeRepoProvider
from tests.helpers import Sessions, all_jobs
from tests.team_flow import (
    F01_TEST,
    START,
    Flow,
    api_with,
    approve_all,
    drafted,
    save_claims,
    snapshot,
    started,
)
from worker.run_specs import RunPlan, final_run_plan

DECLARATION = {
    "starter_code": True,
    "earlier_work": False,
    "ai_assistance": True,
    "notes": "Started from the Express hello-world; Claude helped with the rounding.",
}


async def ready(
    api: Api, clock: FakeClock, sessions: Sessions, **event: Any
) -> tuple[Flow, models.Snapshot, models.TestSpec]:
    """F01's claims saved, its test drafted and approved, and a snapshot."""
    flow = await started(api, clock, **event)
    await save_claims(flow)
    [spec] = await drafted(sessions, flow, F01_TEST)
    await approve_all(flow)
    return flow, await snapshot(sessions, flow), spec


def body(snapshot_id: object, key: str = "submit-key-0001", **overrides: Any) -> dict[str, Any]:
    return {
        "snapshot_id": str(snapshot_id),
        "mode": "sandbox",
        "reuse_declaration": DECLARATION,
        "idempotency_key": key,
        **overrides,
    }


async def submit(
    flow: Flow, snapshot_id: object, key: str = "submit-key-0001", **extra: Any
) -> Any:
    return await flow.client.post("/api/team/submissions", json=body(snapshot_id, key, **extra))


async def submissions(sessions: Sessions) -> list[models.Submission]:
    async with sessions() as session:
        return list(await session.scalars(select(models.Submission)))


def code(response: Any) -> tuple[int, str]:
    return response.status_code, response.json()["detail"]["code"]


async def test_a_submission_returns_a_receipt_and_queues_the_final_run(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow, snap, spec = await ready(api, clock, sessions)
    claims_hash = (await flow.client.get("/api/team/claims")).json()["current"]["claims_hash"]

    response = await submit(flow, snap.id)

    assert response.status_code == 201, response.text
    [row] = await submissions(sessions)
    receipt = response.json()
    assert {k: v for k, v in receipt.items() if k not in ("submitted_at", "receipt_hash")} == {
        "submission_id": str(row.id),
        "team_id": str(flow.team_id),
        "team_name": "Rocket",
        "commit_sha": snap.commit_sha,
        "archive_sha256": snap.archive_sha256,
        "claims_hash": claims_hash,
        "test_spec_hash": hash_json([spec.spec_hash]),
        "rubric_version": 1,
    }
    assert (row.mode, row.claims_version, row.claims_hash, row.snapshot_id) == (
        "sandbox",
        1,
        claims_hash,
        snap.id,
    )
    assert (row.runner_version, row.railpack_version) == ("1", "0.43.0")
    assert row.reuse_declaration == DECLARATION
    assert row.idempotency_key == f"{flow.team_id}:submit-key-0001"
    [job] = [job for job in await all_jobs(sessions) if job.kind == "final_run"]
    assert (job.submission_id, job.team_id, job.idempotency_key) == (
        row.id,
        flow.team_id,
        queue.final_run_key(row.id),
    )


async def test_a_submission_queues_an_audit_after_its_final_run(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow, snap, _ = await ready(api, clock, sessions)

    assert (await submit(flow, snap.id)).status_code == 201

    [row] = await submissions(sessions)
    jobs = await all_jobs(sessions)
    [final_run] = [job for job in jobs if job.kind == "final_run"]
    [audit] = [job for job in jobs if job.kind == "audit"]
    assert (audit.submission_id, audit.team_id, audit.idempotency_key, audit.depends_on) == (
        row.id,
        flow.team_id,
        queue.audit_key(row.id),
        final_run.id,
    )


async def test_the_receipt_hash_recomputes_from_the_stored_row(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow, snap, _ = await ready(api, clock, sessions)

    receipt = (await submit(flow, snap.id)).json()

    [row] = await submissions(sessions)
    fields = {k: v for k, v in receipt.items() if k not in ("team_name", "receipt_hash")}
    assert receipt_hash(receipt_fields(row, snap)) == row.receipt_hash == receipt["receipt_hash"]
    assert hash_json(fields) == receipt["receipt_hash"]  # from the receipt alone


async def test_the_final_run_finds_exactly_the_approved_tests(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow, snap, spec = await ready(api, clock, sessions)
    await submit(flow, snap.id)

    [row] = await submissions(sessions)
    async with sessions() as session:
        plan = await final_run_plan(session, row)

    assert isinstance(plan, RunPlan)
    assert [str(document["spec_id"]) for document in plan.runnable] == [str(spec.id)]


async def test_submitting_twice_with_one_key_gives_one_receipt(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow, snap, _ = await ready(api, clock, sessions)

    first = await submit(flow, snap.id)
    again = await submit(flow, snap.id)

    assert (first.status_code, again.status_code) == (201, 200)
    assert first.json() == again.json()
    assert len(await submissions(sessions)) == 1


async def test_two_simultaneous_submits_with_one_key_store_one_submission(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow, snap, _ = await ready(api, clock, sessions)
    # Leave two open database connections, so neither request waits to connect.
    await asyncio.gather(*(flow.client.get("/api/team/tests") for _ in range(2)))

    one, two = await asyncio.gather(submit(flow, snap.id), submit(flow, snap.id))

    assert sorted((one.status_code, two.status_code)) == [200, 201], (one.text, two.text)
    assert one.json() == two.json()
    assert len(await submissions(sessions)) == 1
    kinds = [job.kind for job in await all_jobs(sessions)]
    assert (kinds.count("final_run"), kinds.count("audit")) == (1, 1)


async def test_a_new_commit_needs_no_new_approval(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow, snap, _ = await ready(api, clock, sessions)
    first = (await submit(flow, snap.id)).json()
    later = await snapshot(sessions, flow)

    second = await submit(flow, later.id, "submit-key-0002")
    current = (await flow.client.get("/api/team/submissions/current")).json()

    assert second.status_code == 201, second.text
    assert current["receipt"] == second.json()
    assert current["receipt"]["commit_sha"] == later.commit_sha
    stored = {row.id: row.receipt_hash for row in await submissions(sessions)}
    assert stored[uuid.UUID(first["submission_id"])] == first["receipt_hash"]


async def test_a_submission_after_the_deadline_is_refused(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    # The event ended a minute ago by the database's clock; the API's clock is still inside it.
    flow, snap, _ = await ready(api, clock, sessions, ends_in=timedelta(minutes=-1))

    response = await submit(flow, snap.id)

    assert code(response) == (409, "deadline_passed")
    assert await submissions(sessions) == []


async def test_a_repeated_key_still_returns_its_receipt_after_the_deadline(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow, snap, _ = await ready(api, clock, sessions)
    first = await submit(flow, snap.id)
    async with sessions.begin() as session:
        await session.execute(text("UPDATE events SET deadline_at = now() - interval '1 minute'"))

    again = await submit(flow, snap.id)
    other_key = await submit(flow, snap.id, "submit-key-0002")

    assert (again.status_code, again.json()) == (200, first.json())
    assert code(other_key) == (409, "deadline_passed")


async def test_a_claims_edit_after_approval_needs_new_approval(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow, snap, _ = await ready(api, clock, sessions)
    await save_claims(flow, {**START, "explanation": "Edited in another tab."})
    await drafted(sessions, flow, F01_TEST)

    response = await submit(flow, snap.id)

    assert code(response) == (409, "tests_not_approved")
    assert response.json()["detail"]["claims"] == ["c1"]
    assert await submissions(sessions) == []


CASES = {
    "no_claims": (409, "no_claims"),
    "still_drafting": (409, "tests_not_ready"),
    "drafting_failed": (409, "tests_not_ready"),
    "a_draft_left": (409, "tests_not_approved"),
    "another_teams_snapshot": (404, "snapshot_not_found"),
    "url_mode": (422, None),
}


@pytest.mark.parametrize("case", list(CASES))
async def test_what_cannot_be_submitted(
    api: Api, clock: FakeClock, sessions: Sessions, case: str
) -> None:
    flow = await started(api, clock)
    snap = await snapshot(sessions, flow)
    if case != "no_claims":
        await save_claims(flow)
    if case == "drafting_failed":
        await drafted(sessions, flow, job="failed")
    if case in ("a_draft_left", "another_teams_snapshot", "url_mode"):
        await drafted(sessions, flow, F01_TEST)
    if case in ("another_teams_snapshot", "url_mode"):
        await approve_all(flow)
    if case == "another_teams_snapshot":
        other = await started(api, clock, name="Comet", organiser=flow.organiser, event=flow.event)
        snap = await snapshot(sessions, other)

    response = await submit(flow, snap.id, mode="url" if case == "url_mode" else "sandbox")

    status, expected = CASES[case]
    assert response.status_code == status, response.text
    if expected is not None:
        assert response.json()["detail"]["code"] == expected
    assert await submissions(sessions) == []


async def test_the_number_of_submissions_is_capped(
    sessions: Sessions,
    migrated_database_url: str,
    tmp_path: Path,
    clock: FakeClock,
    repos: FakeRepoProvider,
) -> None:
    async with api_with(migrated_database_url, tmp_path, clock, repos, max_submissions=1) as api:
        flow, snap, _ = await ready(api, clock, sessions)
        first = await submit(flow, snap.id)
        second = await submit(flow, snap.id, "submit-key-0002")
        repeat = await submit(flow, snap.id)

    assert first.status_code == 201
    assert code(second) == (409, "too_many_submissions")
    assert repeat.status_code == 200  # the first key still answers
    assert len(await submissions(sessions)) == 1


async def test_unclear_and_ui_claims_do_not_block(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow = await started(api, clock)
    unclear = {"text": "Splits it fairly.", "target": "POST /split"}
    page = {"text": "Shows a page of past splits.", "target": "/history"}
    await save_claims(flow, {**START, "items": [START["items"][0], unclear, page]})
    first, _ = await drafted(
        sessions,
        flow,
        F01_TEST,
        {**F01_TEST, "claim_id": "c2", "status": "needs_clarification", "reason": "Fair how?"},
    )
    await approve_all(flow)
    snap = await snapshot(sessions, flow)

    response = await submit(flow, snap.id)

    assert response.status_code == 201, response.text
    assert response.json()["test_spec_hash"] == hash_json([first.spec_hash])


async def test_a_team_with_no_approved_test_can_still_submit(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow = await started(api, clock)
    await save_claims(flow)
    await drafted(sessions, flow, {**F01_TEST, "status": "needs_clarification", "reason": "?"})
    snap = await snapshot(sessions, flow)

    response = await submit(flow, snap.id)

    assert response.status_code == 201, response.text
    assert response.json()["test_spec_hash"] == hash_json([])


async def test_the_current_submission_is_the_latest_by_the_deadline(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow, snap, _ = await ready(api, clock, sessions)
    before = await flow.client.get("/api/team/submissions/current")
    await submit(flow, snap.id)
    second = (await submit(flow, snap.id, "submit-key-0002")).json()

    current = (await flow.client.get("/api/team/submissions/current")).json()

    assert code(before) == (404, "no_submission")
    assert current == {"receipt": second, "submissions_made": 2, "final_run": "queued"}


async def test_another_team_has_no_current_submission(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow, snap, _ = await ready(api, clock, sessions)
    other = await started(api, clock, name="Comet", organiser=flow.organiser, event=flow.event)
    await submit(flow, snap.id)

    response = await other.client.get("/api/team/submissions/current")

    assert code(response) == (404, "no_submission")
