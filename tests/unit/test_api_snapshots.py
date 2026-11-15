"""A team snapshots its head commit or a pasted one (M7, part 4)."""

from datetime import timedelta

import pytest
from sqlalchemy import text

from core import queue
from core.errors import InfraError
from tests.api_helpers import Api
from tests.fakes import FakeClock, FakeRepoProvider
from tests.helpers import Sessions, all_jobs, enqueue
from tests.team_flow import at, move_to, snapshot, started, with_repo

SHA = "a" * 40


async def test_the_head_commit_is_snapshotted_by_default(
    api: Api, clock: FakeClock, repos: FakeRepoProvider, sessions: Sessions
) -> None:
    flow = await started(api, clock)
    await with_repo(flow, repos, head=SHA)

    response = await flow.client.post("/api/team/snapshots", json={})

    assert response.status_code == 202, response.text
    assert response.json() == {"commit_sha": SHA, "status": "queued", "snapshot_id": None}
    [job] = await all_jobs(sessions)
    assert (job.kind, job.idempotency_key, job.payload, job.team_id) == (
        "prepare_snapshot",
        queue.prepare_key(flow.team_id, SHA),
        {"commit_sha": SHA},
        flow.team_id,
    )


async def test_a_pasted_commit_is_snapshotted(
    api: Api, clock: FakeClock, repos: FakeRepoProvider, sessions: Sessions
) -> None:
    flow = await started(api, clock)
    await with_repo(flow, repos, head=SHA)

    response = await flow.client.post("/api/team/snapshots", json={"commit_sha": "B" * 40})

    assert response.status_code == 202, response.text
    assert response.json()["commit_sha"] == "b" * 40
    [job] = await all_jobs(sessions)
    assert job.payload == {"commit_sha": "b" * 40}


async def test_a_commit_already_snapshotted_is_ready(
    api: Api, clock: FakeClock, repos: FakeRepoProvider, sessions: Sessions
) -> None:
    flow = await started(api, clock)
    await with_repo(flow, repos, head=SHA)
    taken = await snapshot(sessions, flow, commit_sha=SHA)

    response = await flow.client.post("/api/team/snapshots", json={})

    assert response.status_code == 200
    assert response.json() == {"commit_sha": SHA, "status": "ready", "snapshot_id": str(taken.id)}
    assert await all_jobs(sessions) == []


async def test_asking_twice_makes_one_job(
    api: Api, clock: FakeClock, repos: FakeRepoProvider, sessions: Sessions
) -> None:
    flow = await started(api, clock)
    await with_repo(flow, repos, head=SHA)

    first = await flow.client.post("/api/team/snapshots", json={})
    second = await flow.client.post("/api/team/snapshots", json={"commit_sha": SHA})

    assert (
        first.json()
        == second.json()
        == {"commit_sha": SHA, "status": "queued", "snapshot_id": None}
    )
    assert len(await all_jobs(sessions)) == 1


async def test_a_refused_commit_says_why_when_asked_again(
    api: Api, clock: FakeClock, repos: FakeRepoProvider, sessions: Sessions
) -> None:
    flow = await started(api, clock)
    await with_repo(flow, repos, head=SHA)
    await flow.client.post("/api/team/snapshots", json={})
    async with sessions.begin() as session:
        await session.execute(
            text(
                "UPDATE jobs SET status = 'done', error_class = 'app', "
                "error_detail = 'the repository has no commit' WHERE kind = 'prepare_snapshot'"
            )
        )

    response = await flow.client.post("/api/team/snapshots", json={})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "snapshot_refused"
    assert "no commit" in response.json()["detail"]["message"]


CASES = {
    "no_repo": (409, "repo_not_registered"),
    "short_sha": (422, "commit_invalid"),
    "empty_repo": (409, "repo_empty"),
    "host_down": (503, "repo_provider_unavailable"),
    "after_deadline": (409, "deadline_passed"),
}


@pytest.mark.parametrize("case", list(CASES))
async def test_what_cannot_be_snapshotted(
    api: Api, clock: FakeClock, repos: FakeRepoProvider, sessions: Sessions, case: str
) -> None:
    flow = await started(api, clock, kickoff_ago=timedelta(hours=1), ends_in=timedelta(hours=2))
    head: str | Exception | None = {"empty_repo": None, "host_down": InfraError("down")}.get(
        case, SHA
    )
    if case != "no_repo":
        await with_repo(flow, repos, head=head)
    if case == "after_deadline":
        move_to(clock, at(flow.event["deadline_at"]) + timedelta(seconds=1))
    body = {"commit_sha": "abc1234"} if case == "short_sha" else {}

    response = await flow.client.post("/api/team/snapshots", json=body)

    assert (response.status_code, response.json()["detail"]["code"]) == CASES[case]
    assert await all_jobs(sessions) == []


async def _finish(sessions: Sessions, sha: str, status: str, detail: str | None) -> None:
    async with sessions.begin() as session:
        await session.execute(
            text(
                "UPDATE jobs SET status = :status, finished_at = now(), "
                "error_class = CASE WHEN CAST(:detail AS text) IS NULL THEN NULL "
                "WHEN :status = 'done' THEN 'app' ELSE 'infra' END, "
                "error_detail = CAST(:detail AS text) WHERE payload->>'commit_sha' = :sha"
            ),
            {"status": status, "detail": detail, "sha": sha},
        )


async def test_the_list_shows_snapshots_and_commits_in_progress(
    api: Api, clock: FakeClock, repos: FakeRepoProvider, sessions: Sessions
) -> None:
    flow = await started(api, clock)
    await with_repo(flow, repos, head=SHA)
    older = await snapshot(sessions, flow, created_at=clock.now() - timedelta(minutes=5))
    newer = await snapshot(sessions, flow, created_at=clock.now())
    # The job that made the newer snapshot is done: its commit is a snapshot, not pending.
    await enqueue(
        sessions,
        "prepare_snapshot",
        queue.prepare_key(flow.team_id, newer.commit_sha),
        payload={"commit_sha": newer.commit_sha},
        team_id=flow.team_id,
    )
    for sha in ("d" * 40, "e" * 40, "f" * 40):
        assert (
            await flow.client.post("/api/team/snapshots", json={"commit_sha": sha})
        ).status_code == 202
    await _finish(sessions, "e" * 40, "done", "commit not found")
    await _finish(sessions, "f" * 40, "failed", "GitHub could not be reached")
    await _finish(sessions, newer.commit_sha, "done", None)

    response = await flow.client.get("/api/team/snapshots")

    assert response.status_code == 200
    body = response.json()
    assert [(s["id"], s["commit_sha"]) for s in body["snapshots"]] == [
        (str(newer.id), newer.commit_sha),
        (str(older.id), older.commit_sha),
    ]
    assert at(body["snapshots"][0]["created_at"]) == newer.created_at
    assert body["pending"] == [
        {"commit_sha": "f" * 40, "status": "failed", "detail": "GitHub could not be reached"},
        {"commit_sha": "e" * 40, "status": "refused", "detail": "commit not found"},
        {"commit_sha": "d" * 40, "status": "queued", "detail": None},
    ]


async def test_another_teams_snapshots_are_not_listed(
    api: Api, clock: FakeClock, repos: FakeRepoProvider, sessions: Sessions
) -> None:
    flow = await started(api, clock)
    other = await started(api, clock, name="Comet", organiser=flow.organiser, event=flow.event)
    await with_repo(flow, repos, head=SHA)
    await snapshot(sessions, flow)
    await flow.client.post("/api/team/snapshots", json={})

    response = await other.client.get("/api/team/snapshots")

    assert response.json() == {"snapshots": [], "pending": []}
