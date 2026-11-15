"""A team self-checks a snapshot and reads the result (M7, part 5)."""

import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from core import queue
from core.db import models
from tests.api_helpers import Api
from tests.fakes import FakeClock
from tests.helpers import Sessions, all_jobs, enqueue
from tests.team_flow import (
    F01_TEST,
    HIDDEN,
    Flow,
    at,
    drafted,
    finished_self_check,
    move_to,
    save_claims,
    snapshot,
    started,
)

REQUEST = "request-0001"


async def ready(
    api: Api, clock: FakeClock, sessions: Sessions, **event: Any
) -> tuple[Flow, models.Snapshot, models.TestSpec]:
    """A team with F01's claim, its drafted test, and one snapshot."""
    flow = await started(api, clock, **event)
    await save_claims(flow)
    [spec] = await drafted(sessions, flow, F01_TEST)
    return flow, await snapshot(sessions, flow), spec


async def start(flow: Flow, snapshot_id: object, request_id: str = REQUEST) -> Any:
    return await flow.client.post(
        f"/api/team/snapshots/{snapshot_id}/self-check", json={"client_request_id": request_id}
    )


async def started_check(flow: Flow, snapshot_id: object, request_id: str = REQUEST) -> uuid.UUID:
    response = await start(flow, snapshot_id, request_id)
    assert response.status_code == 202, response.text
    return uuid.UUID(response.json()["job_id"])


async def self_check_jobs(sessions: Sessions) -> list[models.Job]:
    return [job for job in await all_jobs(sessions) if job.kind == "self_check_run"]


async def test_a_self_check_is_queued_for_the_snapshot(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow, snap, _ = await ready(api, clock, sessions)

    response = await start(flow, snap.id)

    assert response.status_code == 202, response.text
    [job] = await self_check_jobs(sessions)
    assert response.json() == {"job_id": str(job.id), "status": "queued"}
    assert (job.snapshot_id, job.team_id, job.idempotency_key) == (
        snap.id,
        flow.team_id,
        queue.self_check_key(snap.id, REQUEST),
    )


async def test_the_same_request_is_the_same_job(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow, snap, _ = await ready(api, clock, sessions)

    first = await started_check(flow, snap.id)
    again = await started_check(flow, snap.id)

    assert first == again
    assert len(await self_check_jobs(sessions)) == 1


async def test_one_self_check_at_a_time(api: Api, clock: FakeClock, sessions: Sessions) -> None:
    flow, snap, _ = await ready(api, clock, sessions)
    first = await started_check(flow, snap.id)

    busy = await start(flow, snap.id, "request-0002")
    async with sessions.begin() as session:
        await session.execute(text("UPDATE jobs SET status = 'done' WHERE id = :id"), {"id": first})
    after = await start(flow, snap.id, "request-0002")

    assert busy.status_code == 409
    assert busy.json()["detail"]["code"] == "self_check_in_progress"
    assert busy.json()["detail"]["job_id"] == str(first)
    assert after.status_code == 202


@pytest.mark.parametrize(
    ("case", "expected"),
    [("another_teams_snapshot", 404), ("after_deadline", 409), ("short_request_id", 422)],
)
async def test_what_cannot_be_self_checked(
    api: Api, clock: FakeClock, sessions: Sessions, case: str, expected: int
) -> None:
    flow, snap, _ = await ready(
        api, clock, sessions, kickoff_ago=timedelta(hours=1), ends_in=timedelta(hours=2)
    )
    actor = flow
    if case == "another_teams_snapshot":
        actor = await started(api, clock, name="Comet", organiser=flow.organiser, event=flow.event)
    if case == "after_deadline":
        move_to(clock, at(flow.event["deadline_at"]) + timedelta(seconds=1))

    response = await start(actor, snap.id, "short" if case == "short_request_id" else REQUEST)

    assert response.status_code == expected
    if case == "after_deadline":
        assert response.json()["detail"]["code"] == "deadline_passed"
    assert await self_check_jobs(sessions) == []


async def test_a_running_self_check_has_no_results_yet(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow, snap, _ = await ready(api, clock, sessions)
    job_id = await started_check(flow, snap.id)

    response = await flow.client.get(f"/api/team/self-checks/{job_id}")

    assert response.status_code == 200
    assert response.json() == {
        "job_id": str(job_id),
        "snapshot_id": str(snap.id),
        "commit_sha": snap.commit_sha,
        "status": "queued",
        "error": None,
        "run": None,
        "results": [],
        "claim_verdicts": {},
    }


async def test_a_finished_self_check_shows_the_run_and_each_test(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow, snap, spec = await ready(api, clock, sessions)
    job_id = await started_check(flow, snap.id)
    await finished_self_check(sessions, job_id, results=[(spec, "p1", "pass")])

    response = await flow.client.get(f"/api/team/self-checks/{job_id}")

    body = response.json()
    assert body["status"] == "done"
    assert body["run"] == {
        "build_status": "ok", "startup_status": "ok", "startup_detail": None, "model_use": None,
    }  # fmt: skip
    assert body["results"][0].pop("result_id")
    assert body["results"] == [
        {
            "kind": "api",
            "steps": None,
            "page": None,
            "has_video": False,
            "has_trace": False,
            "claim_id": "c1",
            "case_id": "p1",
            "verdict": "pass",
            "assertion_results": [
                {"op": "status_equals", "expected": 200, "actual": 200, "passed": True}
            ],
            "error": None,
            "duration_ms": 12,
        }
    ]
    assert body["claim_verdicts"] == {"c1": "pass"}
    assert str(HIDDEN) not in response.text
    assert "final_cases" not in response.text


async def test_a_blocked_run_says_why(api: Api, clock: FakeClock, sessions: Sessions) -> None:
    flow, snap, spec = await ready(api, clock, sessions)
    job_id = await started_check(flow, snap.id)
    await finished_self_check(
        sessions,
        job_id,
        results=[(spec, "p1", "blocked")],
        startup="fail",
        startup_detail="nothing answered on / within 60 s",
    )

    body = (await flow.client.get(f"/api/team/self-checks/{job_id}")).json()

    assert body["run"]["startup_status"] == "fail"
    assert body["run"]["startup_detail"] == "nothing answered on / within 60 s"
    assert body["claim_verdicts"] == {"c1": "blocked"}


async def test_only_a_teams_own_self_check_is_readable(
    api: Api, clock: FakeClock, sessions: Sessions, tmp_path: Path
) -> None:
    flow, snap, spec = await ready(api, clock, sessions)
    other = await started(api, clock, name="Comet", organiser=flow.organiser, event=flow.event)
    job_id = await started_check(flow, snap.id)
    final = await enqueue(
        sessions, "final_run", f"final_run:{uuid.uuid4()}", team_id=flow.team_id,
        snapshot_id=snap.id,
    )  # fmt: skip
    log = tmp_path / "runs" / "final" / "app.log"
    log.parent.mkdir(parents=True)
    log.write_text("POST /split {'n': 987654321}\n")
    await finished_self_check(
        sessions, final, results=[(spec, "f1", "pass")], logs={"build": log, "app": log}
    )

    responses = {
        "B reads A's check": await other.client.get(f"/api/team/self-checks/{job_id}"),
        "B reads A's log": await other.client.get(f"/api/team/self-checks/{job_id}/logs/app"),
        "A reads its final run": await flow.client.get(f"/api/team/self-checks/{final}"),
        "A reads its final build log": await flow.client.get(
            f"/api/team/self-checks/{final}/logs/build"
        ),
        "A reads its final app log": await flow.client.get(
            f"/api/team/self-checks/{final}/logs/app"
        ),
    }

    assert {name: r.status_code for name, r in responses.items()} == dict.fromkeys(responses, 404)
    assert all(str(HIDDEN) not in r.text for r in responses.values())


async def test_only_the_end_of_a_log_inside_the_artifact_directory_is_served(
    api: Api,
    clock: FakeClock,
    sessions: Sessions,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    flow, snap, _ = await ready(api, clock, sessions)
    runs = tmp_path / "runs" / "attempt"
    runs.mkdir(parents=True)
    app_log = runs / "app.log"
    app_log.write_text("".join(f"line {n}\n" for n in range(1, 301)))
    outside = tmp_path_factory.mktemp("elsewhere") / "build.log"
    outside.write_text("a secret from another directory\n")
    job_id = await started_check(flow, snap.id)
    await finished_self_check(sessions, job_id, logs={"app": app_log, "build": outside})
    gone_job = await started_check(flow, snap.id, "request-0002")  # the first one is done
    await finished_self_check(sessions, gone_job, logs={"app": runs / "deleted.log"})
    huge_job = await started_check(flow, snap.id, "request-0003")
    huge_log = runs / "huge.log"
    huge_log.write_text("".join(f"{n:04d} {'x' * 995}\n" for n in range(1000)))  # ~1 MB
    await finished_self_check(sessions, huge_job, logs={"app": huge_log})

    app = await flow.client.get(f"/api/team/self-checks/{job_id}/logs/app")
    build = await flow.client.get(f"/api/team/self-checks/{job_id}/logs/build")
    final = await flow.client.get(f"/api/team/self-checks/{job_id}/logs/final")
    missing = await flow.client.get(f"/api/team/self-checks/{gone_job}/logs/app")
    huge = await flow.client.get(f"/api/team/self-checks/{huge_job}/logs/app")

    assert app.status_code == 200
    assert app.headers["content-type"].startswith("text/plain")
    assert app.text.splitlines() == [f"line {n}" for n in range(101, 301)]
    assert (build.status_code, final.status_code, missing.status_code) == (404, 404, 404)
    assert "secret" not in build.text
    assert huge.status_code == 200
    assert len(huge.content) <= 64 * 1024
    lines = huge.text.splitlines()
    assert lines[-1].startswith("0999 ") and all(len(line) == 1000 for line in lines)


MODEL_USE = {
    "door": "prune", "calls": 3, "calls_ok": 2,
    "refused": [{"reason": "this run's model budget was used up", "count": 1}],
    "models": {"gpt-4.1-nano": 2}, "tokens": {"prompt_tokens": 20}, "cost_usd": 0.0004,
    "optimised": False,
    "outside": {"connections": 4, "refused": 1, "hosts": {"api.weather.test": 4}},
    "receipt": {"run_id": "r", "steps": [{"n": 1, "model": "gpt-4.1-nano"}]},
    "receipt_verified": True,
}  # fmt: skip


async def test_a_self_check_shows_what_the_app_used_through_the_model_door(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow, snap, spec = await ready(api, clock, sessions)
    job_id = await started_check(flow, snap.id)
    await finished_self_check(sessions, job_id, results=[(spec, "p1", "pass")], model_use=MODEL_USE)

    body = (await flow.client.get(f"/api/team/self-checks/{job_id}")).json()

    assert body["run"]["model_use"] == {
        "calls": 3, "calls_ok": 2,
        "refused": [{"reason": "this run's model budget was used up", "count": 1}],
        "models": {"gpt-4.1-nano": 2}, "outside_connections": 4, "outside_refused": 1,
        "outside_hosts": {"api.weather.test": 4}, "receipt_verified": True,
    }  # fmt: skip
    assert "steps" not in str(body["run"]["model_use"])  # the receipt itself is not sent
