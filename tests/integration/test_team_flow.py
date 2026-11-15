"""A team's whole flow, through the API and the worker's own handlers (M7).

The team saves F01's start text and claim; the draft job drafts its test from the recorded
model reply (no network, no cost); the team approves it, snapshots the head of the F01 good
fixture remote, self-checks it, and submits; the final run passes c1. Runs inside the worker
image against `hp_test`, with run folders under HOST_ARTIFACT_DIR.
"""

import contextlib
import shutil
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import docker
import docker.errors
import pytest
from sqlalchemy import select

from core import queue
from core.clock import SystemClock
from core.db import models
from core.git import run_git
from core.providers.local import LocalGitProvider
from core.results import submission_verdicts
from core.settings import Settings
from scripts.fixtures import load_fixtures
from tests.fakes import FakeClock
from tests.helpers import Sessions
from tests.team_flow import START, api_with, approve_all, save_claims, started
from worker.build import image_tag
from worker.lanes import Handler
from worker.main import build_handlers
from worker.runtime import DockerRuntime

pytestmark = pytest.mark.integration

F01 = next(fixture for fixture in load_fixtures() if fixture.id == "F01")


@dataclass
class Worker:
    settings: Settings
    handlers: dict[str, Handler]
    runs: list[tuple[uuid.UUID, uuid.UUID]] = field(default_factory=list)  # (team, attempt)

    async def run_next(self, sessions: Sessions, kind: str) -> queue.Job:
        """Claim the next heavy job, which must be `kind`, run it and complete it."""
        async with sessions.begin() as session:
            job = await queue.claim(session, "heavy")
        assert job is not None and job.kind == kind, job
        outcome = await self.handlers[kind](job)
        assert outcome is None, outcome
        async with sessions.begin() as session:
            assert await queue.complete(session, job.id, job.attempt_id)
        if kind in ("self_check_run", "final_run") and job.team_id is not None:
            self.runs.append((job.team_id, job.attempt_id))
        return job


@pytest.fixture
async def worker(sessions: Sessions) -> AsyncIterator[Worker]:
    """The worker's handlers with replayed model replies; removes what the runs leave behind."""
    settings = Settings(repo_provider="local", llm_mode="replay")
    archives = settings.artifact_dir / "archives"
    archives_before = set(archives.iterdir()) if archives.is_dir() else set()
    harness = Worker(settings, build_handlers(settings, sessions, SystemClock(), DockerRuntime()))
    try:
        yield harness
    finally:
        client = docker.from_env()
        for team_id, attempt_id in harness.runs:
            with contextlib.suppress(docker.errors.ImageNotFound):
                client.images.remove(image_tag(team_id, attempt_id), force=True)
            shutil.rmtree(settings.artifact_dir / "runs" / str(attempt_id), ignore_errors=True)
        client.close()
        archives_after = set(archives.iterdir()) if archives.is_dir() else set()
        for archive in archives_after - archives_before:
            archive.unlink(missing_ok=True)


def ok(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


async def test_a_team_goes_from_its_claims_to_a_passing_final_run(
    sessions: Sessions, worker: Worker, migrated_database_url: str
) -> None:
    clock = FakeClock(SystemClock().now())
    settings = worker.settings
    remote = F01.variant("good").remote_name
    provider = LocalGitProvider(settings.local_remotes_dir, clock)
    artifacts: Path = settings.artifact_dir
    async with api_with(
        migrated_database_url,
        artifacts,
        clock,
        provider,
        repo_provider="local",  # type: ignore[arg-type]
    ) as api:
        flow = await started(api, clock)
        team = flow.client
        ok(
            await team.post(
                "/api/team/repo",
                json={"repo_url": f"local://{remote}", "health_path": F01.health_path},
            )
        )

        # Claims, and the test drafted from them.
        await save_claims(flow, START)
        await worker.run_next(sessions, "draft_specs")
        tests = ok(await team.get("/api/team/tests"))
        assert (tests["drafting"], [c["state"] for c in tests["claims"]]) == ("done", ["draft"])
        await approve_all(flow)
        assert [c["state"] for c in ok(await team.get("/api/team/tests"))["claims"]] == ["approved"]

        # A snapshot of the default branch's head.
        head = await run_git(
            "rev-parse", "main^{commit}", git_dir=settings.local_remotes_dir / f"{remote}.git"
        )
        asked = ok(await team.post("/api/team/snapshots", json={}), 202)
        assert asked["commit_sha"] == head
        await worker.run_next(sessions, "prepare_snapshot")
        [snapshot] = ok(await team.get("/api/team/snapshots"))["snapshots"]
        assert snapshot["commit_sha"] == head

        # A self-check of it.
        check = ok(
            await team.post(
                f"/api/team/snapshots/{snapshot['id']}/self-check",
                json={"client_request_id": "request-0001"},
            ),
            202,
        )
        await worker.run_next(sessions, "self_check_run")
        result = ok(await team.get(f"/api/team/self-checks/{check['job_id']}"))
        assert (result["status"], result["run"]["startup_status"]) == ("done", "ok")
        assert result["claim_verdicts"] == {"c1": "pass"}
        build_log = await team.get(f"/api/team/self-checks/{check['job_id']}/logs/build")
        assert build_log.status_code == 200 and build_log.text

        # The submission, and its final run.
        receipt = ok(
            await team.post(
                "/api/team/submissions",
                json={
                    "snapshot_id": snapshot["id"],
                    "mode": "sandbox",
                    "reuse_declaration": {
                        "starter_code": False, "earlier_work": False, "ai_assistance": True,
                    },
                    "idempotency_key": "submit-key-0001",
                },
            ),
            201,
        )  # fmt: skip
        assert receipt["commit_sha"] == head
        job = await worker.run_next(sessions, "final_run")
        assert job.submission_id == uuid.UUID(receipt["submission_id"])
        current = ok(await team.get("/api/team/submissions/current"))
        assert (current["receipt"], current["final_run"]) == (receipt, "done")
        overview = ok(await team.get("/api/team"))["progress"]
        assert overview == {
            "has_claims": True,
            "tests_approved": 1,
            "tests_total": 1,
            "has_snapshot": True,
            "has_submitted": True,
        }

    async with sessions() as session:
        submission = await session.scalar(select(models.Submission))
        assert submission is not None
        assert await submission_verdicts(session, submission.id) == {"c1": "pass"}
