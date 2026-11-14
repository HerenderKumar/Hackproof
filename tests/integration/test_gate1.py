"""Gate 1: one fixture end to end through the database (M5).

F01's good and broken variants go through the worker's own handlers. `prepare_snapshot`
snapshots the fixture remote with the local provider; `final_run` builds the snapshot, starts
it and runs F01's hand-written test; the verdict is read back with `submission_verdicts`, the
way the judge's view will read it. Runs inside the worker image against `hp_test`, with run
folders under HOST_ARTIFACT_DIR and the fixture remotes from LOCAL_REMOTES_DIR.
"""

import contextlib
import shutil
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import docker
import docker.errors
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core import queue
from core.clock import SystemClock
from core.db import models
from core.git import run_git
from core.results import submission_verdicts
from core.settings import Settings
from core.specs import test_spec_hash
from scripts.fixtures import load_fixtures
from tests.factories import T0, make_claims, make_event, make_submission, make_team, make_test_spec
from worker.build import image_tag
from worker.lanes import Handler
from worker.main import build_handlers
from worker.runtime import DockerRuntime

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]
F01 = next(fixture for fixture in load_fixtures() if fixture.id == "F01")


@dataclass
class Gate:
    settings: Settings
    handlers: dict[str, Handler]
    runs: list[tuple[uuid.UUID, uuid.UUID]] = field(default_factory=list)  # (team, attempt)


@pytest.fixture
def gate(sessions: Sessions) -> Iterator[Gate]:
    """The worker's handlers, reading fixture remotes; removes what the runs leave behind."""
    settings = Settings(repo_provider="local")
    archives = settings.artifact_dir / "archives"
    archives_before = set(archives.iterdir()) if archives.is_dir() else set()
    harness = Gate(settings, build_handlers(settings, sessions, SystemClock(), DockerRuntime()))
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
        for archive in archives_after - archives_before:  # only the archives this test made
            archive.unlink(missing_ok=True)


async def run_one(gate: Gate, sessions: Sessions, kind: str, key: str, **refs: Any) -> queue.Job:
    """Enqueue a job, claim it, run it with the worker's handler and complete it."""
    async with sessions.begin() as session:
        await queue.enqueue(session, kind, key, **refs)
    async with sessions.begin() as session:
        job = await queue.claim(session, "heavy")
    assert job is not None and job.kind == kind

    outcome = await gate.handlers[kind](job)

    assert outcome is None, outcome
    async with sessions.begin() as session:
        assert await queue.complete(session, job.id, job.attempt_id)
    return job


async def registered_team(sessions: Sessions, remote: str) -> models.Team:
    async with sessions.begin() as session:
        event = make_event()
        session.add(event)
        await session.flush()
        team = make_team(
            event.id,
            repo_url=f"local://{remote}",
            repo_provider="local",
            repository_id=remote,
            default_branch="main",
            repo_created_at=T0,
            registered_at=T0,
            health_path=F01.health_path,
        )
        session.add(team)
        await session.flush()
        session.add(make_claims(team.id, F01.claims))
        return team


@pytest.mark.parametrize(("variant", "verdict"), [("good", "pass"), ("broken", "fail")])
async def test_f01_runs_end_to_end_and_its_verdict_is_stored(
    sessions: Sessions, gate: Gate, variant: str, verdict: str
) -> None:
    remote = F01.variant(variant).remote_name
    team = await registered_team(sessions, remote)
    git_dir = gate.settings.local_remotes_dir / f"{remote}.git"
    head = await run_git("rev-parse", "main^{commit}", git_dir=git_dir)

    await run_one(
        gate,
        sessions,
        "prepare_snapshot",
        queue.prepare_key(team.id, head),
        payload={"commit_sha": head},
        team_id=team.id,
    )

    async with sessions.begin() as session:
        snapshot = await session.scalar(
            select(models.Snapshot).where(models.Snapshot.team_id == team.id)
        )
        assert snapshot is not None and snapshot.commit_sha == head
        claims = await session.scalar(select(models.Claims).where(models.Claims.team_id == team.id))
        assert claims is not None
        [spec_document] = F01.specs
        spec = make_test_spec(team.id, "c1", document=spec_document)
        session.add(spec)
        submission = make_submission(
            team.id,
            snapshot.id,
            claims_hash=claims.claims_hash,
            test_spec_hash=test_spec_hash([spec.spec_hash]),
        )
        session.add(submission)

    job = await run_one(
        gate,
        sessions,
        "final_run",
        f"final_run:{submission.id}",
        team_id=team.id,
        submission_id=submission.id,
    )
    gate.runs.append((team.id, job.attempt_id))

    async with sessions() as session:
        assert await submission_verdicts(session, submission.id) == {"c1": verdict}
        rows = (
            await session.scalars(
                select(models.TestResult).where(models.TestResult.job_id == job.id)
            )
        ).all()

    assert sorted(row.case_id for row in rows) == ["f1", "f2", "f3"]  # never the practice case
    assert all(row.attempt_id == job.attempt_id for row in rows)
    if variant == "broken":
        [f1] = [row for row in rows if row.case_id == "f1"]
        assert f1.verdict == "fail"
        sums = [a for a in f1.assertion_results if a["op"] == "sum_equals"]
        assert sums == [
            {
                "op": "sum_equals",
                "path": "$.shares[*]",
                "expected": "100.0",
                "actual": "99.99",
                "passed": False,
                "note": None,
                "step": "split",
            }
        ]
