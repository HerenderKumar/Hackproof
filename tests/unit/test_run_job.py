"""The run job: snapshot archive → build → run → `run_results` (sections 11.6, 12.1 to 12.3).

Building and running are faked here; they have their own tests.
"""

import contextlib
import hashlib
import io
import tarfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core import queue
from core.db import models
from core.settings import Settings
from core.specs import test_spec_hash
from tests.factories import (
    T0,
    make_claims,
    make_event,
    make_snapshot,
    make_submission,
    make_team,
    make_test_spec,
)
from tests.fakes import FakeClock, FakeRuntime
from worker.build import BuildResult
from worker.lanes import Outcome
from worker.run_job import RunReport, build_and_run, make_run_handler, startup_spec
from worker.sandbox import RunOutcome, SandboxLimits

Sessions = async_sessionmaker[AsyncSession]
TEAM = uuid.UUID("66666666-6666-6666-6666-666666666666")
ATTEMPT = uuid.UUID("77777777-7777-7777-7777-777777777777")
DIGEST = "sha256:" + "cd" * 32


def make_archive(directory: Path, files: dict[str, str]) -> tuple[Path, str]:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    digest = hashlib.sha256(buffer.getvalue()).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / f"{digest}.tar.gz"
    archive.write_bytes(buffer.getvalue())
    return archive, digest


@dataclass
class FakeBuild:
    status: str = "ok"
    detail: str | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    saw_files: list[str] = field(default_factory=list)

    async def __call__(self, src: Path, scratch: Path, **kwargs: Any) -> BuildResult:
        self.calls.append({"src": src, "scratch": scratch, **kwargs})
        self.saw_files = sorted(p.name for p in src.iterdir())
        ok = self.status == "ok"
        kwargs["log_path"].parent.mkdir(parents=True, exist_ok=True)
        kwargs["log_path"].write_text("build log\n")
        return BuildResult(
            status="ok" if ok else "fail",
            method="railpack",
            exit_code=0 if ok else (None if self.detail == "build timed out" else 1),
            image=f"hp/{kwargs['team_id']}:{kwargs['attempt_id']}" if ok else None,
            image_digest=DIGEST if ok else None,
            build_config={"method": "railpack", "railpack_version": "0.43.0"},
            log_path=kwargs["log_path"],
            detail=self.detail,
        )


@dataclass
class FakeRun:
    startup_status: str = "ok"
    error: Exception | None = None
    tests: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, runtime: object, **kwargs: Any) -> RunOutcome:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        ok = self.startup_status == "ok"
        return RunOutcome(
            startup_status="ok" if ok else "fail",
            startup_detail="HTTP 200 after 1.0s on port 8080"
            if ok
            else "the app exited with code 1 before answering",
            flags_used={
                "root_fallback": False,
                "candidate_ports": [8080],
                "port": 8080 if ok else None,
            },
            results={"runner_version": "1", "startup": {}, "tests": self.tests} if ok else None,
            app_log_path=kwargs["app_log_path"],
            timed_out=False,
        )


LIMITS = SandboxLimits(
    runner_image="hackproof-runner:1",
    app_memory="1g",
    app_cpus=1.0,
    app_pids_limit=256,
    runner_memory="2g",
    startup_timeout_s=60,
    run_timeout_s=600,
)


async def call_build_and_run(
    artifacts: Path, archive: Path, digest: str, build: FakeBuild, run: FakeRun
) -> RunReport:
    return await build_and_run(
        archive_path=archive,
        archive_sha256=digest,
        team_id=TEAM,
        attempt_id=ATTEMPT,
        mode="final",
        specs=[startup_spec("/health")],
        artifact_dir=artifacts,
        runtime=FakeRuntime(),
        build_timeout_s=480,
        buildkit_host="docker-container://buildkit",
        railpack_version="0.43.0",
        limits=LIMITS,
        build=build,
        run=run,
    )


# --- build_and_run -------------------------------------------------------------------------------


async def test_the_snapshot_is_extracted_built_and_run(tmp_path: Path) -> None:
    archive, digest = make_archive(tmp_path / "archives", {"package.json": "{}", "index.js": ""})
    build, run = FakeBuild(), FakeRun()

    report = await call_build_and_run(tmp_path, archive, digest, build, run)

    work = tmp_path / "work" / str(ATTEMPT)
    (built,) = build.calls
    assert built["src"] == work / "src"
    assert built["scratch"] == work / "build"
    assert built["log_path"] == tmp_path / "runs" / str(ATTEMPT) / "build.log"
    assert build.saw_files == ["index.js", "package.json"]
    (ran,) = run.calls
    assert ran["image"] == f"hp/{TEAM}:{ATTEMPT}"
    assert ran["run_dir"] == work / "run"
    assert ran["app_log_path"] == tmp_path / "runs" / str(ATTEMPT) / "app.log"
    assert ran["mode"] == "final"
    assert ran["specs"] == [startup_spec("/health")]
    assert report.build.status == "ok"
    assert report.run is not None and report.run.startup_status == "ok"


async def test_a_failed_build_is_never_run(tmp_path: Path) -> None:
    archive, digest = make_archive(tmp_path / "archives", {"main.py": ""})
    run = FakeRun()

    report = await call_build_and_run(tmp_path, archive, digest, FakeBuild("fail", "x"), run)

    assert run.calls == []
    assert report.run is None


@pytest.mark.parametrize("error", [None, RuntimeError("docker went away")])
async def test_the_work_directory_is_deleted_and_the_logs_kept(
    tmp_path: Path, error: Exception | None
) -> None:
    archive, digest = make_archive(tmp_path / "archives", {"main.py": ""})

    with contextlib.suppress(RuntimeError):  # the run that fails
        await call_build_and_run(tmp_path, archive, digest, FakeBuild(), FakeRun(error=error))

    assert not (tmp_path / "work" / str(ATTEMPT)).exists()
    assert (tmp_path / "runs" / str(ATTEMPT) / "build.log").exists()


def test_the_startup_spec_is_made_by_code_from_the_health_path() -> None:
    assert startup_spec("/healthz") == {
        "kind": "startup",
        "claim_type": "startup",
        "steps": [{"health_path": "/healthz"}],
        "practice_cases": [],
        "final_cases": [],
    }


# --- the job handler -----------------------------------------------------------------------------


@dataclass
class Scene:
    team_id: uuid.UUID
    snapshot_id: uuid.UUID
    submission_id: uuid.UUID
    spec_id: uuid.UUID


async def scene(
    sessions: Sessions, archive: Path, digest: str, *, submission_hash: str | None = None
) -> Scene:
    """A team with claims c1 and c2, an approved test for c1, and a submission pinning it."""
    async with sessions.begin() as session:
        event = make_event()
        session.add(event)
        await session.flush()
        team = make_team(event.id, health_path="/status")
        session.add(team)
        await session.flush()
        snapshot = make_snapshot(team.id, archive_path=str(archive), archive_sha256=digest)
        session.add_all([snapshot, make_claims(team.id)])
        await session.flush()
        spec = make_test_spec(snapshot.id, "c1")
        session.add(spec)
        pinned = submission_hash or test_spec_hash([spec.spec_hash])
        submission = make_submission(team.id, snapshot.id, test_spec_hash=pinned)
        session.add(submission)
        await session.flush()
        return Scene(team.id, snapshot.id, submission.id, spec.id)


async def claim(
    sessions: Sessions,
    kind: str,
    key: str,
    payload: dict[str, Any] | None = None,
    **refs: uuid.UUID,
) -> queue.Job:
    async with sessions.begin() as session:
        await queue.enqueue(session, kind, key, payload=payload, **refs)
    async with sessions.begin() as session:
        job = await queue.claim(session, "heavy")
    assert job is not None
    return job


def handler(sessions: Sessions, tmp_path: Path, build: FakeBuild, run: FakeRun, clock: FakeClock):  # type: ignore[no-untyped-def]
    settings = Settings(host_artifact_dir=tmp_path)
    return make_run_handler(sessions, FakeRuntime(), settings, clock, build=build, run=run)


async def run_results(sessions: Sessions) -> list[models.RunResult]:
    async with sessions() as session:
        return list((await session.scalars(select(models.RunResult))).all())


async def test_a_final_run_records_its_build_and_startup(
    sessions: Sessions, tmp_path: Path
) -> None:
    archive, digest = make_archive(tmp_path / "archives", {"server.js": ""})
    s = await scene(sessions, archive, digest)
    job = await claim(
        sessions,
        "final_run",
        f"final_run:{s.submission_id}",
        team_id=s.team_id,
        submission_id=s.submission_id,
    )
    build, run = FakeBuild(), FakeRun()

    outcome = await handler(sessions, tmp_path, build, run, FakeClock(T0))(job)

    assert outcome is None
    (row,) = await run_results(sessions)
    assert row.job_id == job.id
    assert row.attempt_id == job.attempt_id
    assert row.build_status == "ok"
    assert row.build_exit_code == 0
    assert row.build_method == "railpack"
    assert row.image_digest == DIGEST
    assert row.build_config == {"method": "railpack", "railpack_version": "0.43.0"}
    assert row.startup_status == "ok"
    assert row.startup_detail == "HTTP 200 after 1.0s on port 8080"
    assert row.flags_used == {"root_fallback": False, "candidate_ports": [8080], "port": 8080}
    assert row.build_log_path == str(tmp_path / "runs" / str(job.attempt_id) / "build.log")
    assert row.app_log_path == str(tmp_path / "runs" / str(job.attempt_id) / "app.log")
    assert row.started_at == T0
    assert row.finished_at == T0
    (ran,) = run.calls
    assert ran["mode"] == "final"
    startup, split = ran["specs"]
    assert startup == startup_spec("/status")
    assert split["spec_id"] == str(s.spec_id)


async def test_a_self_check_runs_the_snapshot_in_practice_mode(
    sessions: Sessions, tmp_path: Path
) -> None:
    archive, digest = make_archive(tmp_path / "archives", {"server.js": ""})
    s = await scene(sessions, archive, digest)
    job = await claim(
        sessions, "self_check_run", "selfcheck:1", team_id=s.team_id, snapshot_id=s.snapshot_id
    )
    run = FakeRun()

    await handler(sessions, tmp_path, FakeBuild(), run, FakeClock(T0))(job)

    assert run.calls[0]["mode"] == "practice"
    assert len(await run_results(sessions)) == 1


async def test_a_failed_build_skips_startup_and_says_why(
    sessions: Sessions, tmp_path: Path
) -> None:
    archive, digest = make_archive(tmp_path / "archives", {"main.py": ""})
    s = await scene(sessions, archive, digest)
    job = await claim(
        sessions, "final_run", "final_run:x", team_id=s.team_id, submission_id=s.submission_id
    )

    outcome = await handler(
        sessions, tmp_path, FakeBuild("fail", "build timed out"), FakeRun(), FakeClock(T0)
    )(job)

    assert outcome is None  # a failed build is a result, not a job failure (section 11.3)
    (row,) = await run_results(sessions)
    assert row.build_status == "fail"
    assert row.build_exit_code is None
    assert row.image_digest is None
    assert row.startup_status == "skipped"
    assert row.startup_detail == "not started: build timed out"
    assert row.app_log_path is None


async def test_an_app_that_does_not_start_is_a_result(sessions: Sessions, tmp_path: Path) -> None:
    archive, digest = make_archive(tmp_path / "archives", {"main.py": ""})
    s = await scene(sessions, archive, digest)
    job = await claim(
        sessions, "final_run", "final_run:y", team_id=s.team_id, submission_id=s.submission_id
    )

    outcome = await handler(sessions, tmp_path, FakeBuild(), FakeRun("fail"), FakeClock(T0))(job)

    assert outcome is None
    (row,) = await run_results(sessions)
    assert row.startup_status == "fail"
    assert row.startup_detail == "the app exited with code 1 before answering"


async def test_a_run_job_without_its_snapshot_is_a_validation_error(
    sessions: Sessions, tmp_path: Path
) -> None:
    archive, digest = make_archive(tmp_path / "archives", {"main.py": ""})
    s = await scene(sessions, archive, digest)
    job = await claim(sessions, "self_check_run", "selfcheck:2", team_id=s.team_id)
    build = FakeBuild()

    outcome = await handler(sessions, tmp_path, build, FakeRun(), FakeClock(T0))(job)

    assert outcome == Outcome("validation", "self_check_run needs a snapshot")
    assert build.calls == []
    assert await run_results(sessions) == []


async def test_a_second_write_for_the_same_attempt_is_ignored(
    sessions: Sessions, tmp_path: Path
) -> None:
    archive, digest = make_archive(tmp_path / "archives", {"main.py": ""})
    s = await scene(sessions, archive, digest)
    job = await claim(
        sessions, "final_run", "final_run:z", team_id=s.team_id, submission_id=s.submission_id
    )
    handle = handler(sessions, tmp_path, FakeBuild(), FakeRun(), FakeClock(T0))

    await handle(job)
    await handle(job)

    assert len(await run_results(sessions)) == 1


# --- test results ---------------------------------------------------------------------------------


def runner_entry(spec_id: uuid.UUID, case_id: str, status: str = "pass") -> dict[str, Any]:
    return {
        "spec_id": str(spec_id),
        "case_id": case_id,
        "status": status,
        "error": None,
        "assertions": [],
        "steps": [],
        "request": None,
        "response": None,
        "duration_ms": 5,
        "video_path": None,
        "trace_path": None,
    }


async def test_results(sessions: Sessions) -> list[models.TestResult]:
    async with sessions() as session:
        return list((await session.scalars(select(models.TestResult))).all())


async def test_a_final_run_stores_one_row_per_hidden_case(
    sessions: Sessions, tmp_path: Path
) -> None:
    archive, digest = make_archive(tmp_path / "archives", {"server.js": ""})
    s = await scene(sessions, archive, digest)
    job = await claim(
        sessions, "final_run", "final_run:r", team_id=s.team_id, submission_id=s.submission_id
    )
    run = FakeRun(tests=[runner_entry(s.spec_id, case) for case in ("f1", "f2", "f3")])

    await handler(sessions, tmp_path, FakeBuild(), run, FakeClock(T0))(job)

    rows = await test_results(sessions)
    assert sorted(r.case_id or "" for r in rows) == ["f1", "f2", "f3"]
    assert {(r.job_id, r.attempt_id, r.spec_id, r.verdict) for r in rows} == {
        (job.id, job.attempt_id, s.spec_id, "pass")
    }
    assert run.calls[0]["specs"][1]["practice_cases"] == []


async def test_results_and_run_results_are_written_once_per_attempt(
    sessions: Sessions, tmp_path: Path
) -> None:
    archive, digest = make_archive(tmp_path / "archives", {"server.js": ""})
    s = await scene(sessions, archive, digest)
    job = await claim(
        sessions, "final_run", "final_run:w", team_id=s.team_id, submission_id=s.submission_id
    )
    run = FakeRun(tests=[runner_entry(s.spec_id, case) for case in ("f1", "f2", "f3")])
    handle = handler(sessions, tmp_path, FakeBuild(), run, FakeClock(T0))

    await handle(job)
    await handle(job)

    assert len(await run_results(sessions)) == 1
    assert len(await test_results(sessions)) == 3


async def test_a_final_run_whose_tests_changed_runs_nothing(
    sessions: Sessions, tmp_path: Path
) -> None:
    archive, digest = make_archive(tmp_path / "archives", {"server.js": ""})
    s = await scene(sessions, archive, digest, submission_hash="x" * 64)
    job = await claim(
        sessions, "final_run", "final_run:c", team_id=s.team_id, submission_id=s.submission_id
    )
    build = FakeBuild()

    outcome = await handler(sessions, tmp_path, build, FakeRun(), FakeClock(T0))(job)

    assert outcome == Outcome("validation", "the approved tests do not match the submission")
    assert build.calls == []
    assert await run_results(sessions) == []


async def test_a_self_check_runs_the_specs_in_its_payload(
    sessions: Sessions, tmp_path: Path
) -> None:
    archive, digest = make_archive(tmp_path / "archives", {"server.js": ""})
    s = await scene(sessions, archive, digest)
    job = await claim(
        sessions,
        "self_check_run",
        "selfcheck:p",
        payload={"spec_ids": [str(s.spec_id)]},
        team_id=s.team_id,
        snapshot_id=s.snapshot_id,
    )
    run = FakeRun(tests=[runner_entry(s.spec_id, "p1", "fail")])

    await handler(sessions, tmp_path, FakeBuild(), run, FakeClock(T0))(job)

    (ran,) = run.calls
    assert ran["mode"] == "practice"
    assert [spec["spec_id"] for spec in ran["specs"][1:]] == [str(s.spec_id)]
    assert ran["specs"][1]["final_cases"] == []
    (row,) = await test_results(sessions)
    assert (row.case_id, row.verdict) == ("p1", "fail")


async def test_a_build_failure_blocks_the_tests_it_would_have_run(
    sessions: Sessions, tmp_path: Path
) -> None:
    archive, digest = make_archive(tmp_path / "archives", {"main.py": ""})
    s = await scene(sessions, archive, digest)
    job = await claim(
        sessions, "final_run", "final_run:b", team_id=s.team_id, submission_id=s.submission_id
    )

    await handler(sessions, tmp_path, FakeBuild("fail", "x"), FakeRun(), FakeClock(T0))(job)

    (row,) = await test_results(sessions)
    assert (row.spec_id, row.case_id, row.verdict) == (s.spec_id, None, "blocked")
