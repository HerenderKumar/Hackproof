"""Runs (section 12.3) against a fake runtime: the containers' flags, the fallbacks, cleanup.

Real isolation (no internet, no reach between runs, no inherited environment) is checked
against Docker in tests/integration/test_sandbox.py.
"""

import asyncio
import contextlib
import json
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest

from core.errors import InfraError
from tests.fakes import FakeRuntime
from worker.runtime import ContainerSpec
from worker.sandbox import RunOutcome, SandboxLimits, run_sandboxed

ATTEMPT = uuid.UUID("44444444-4444-4444-4444-444444444444")
IMAGE = f"hp/team:{ATTEMPT}"
RUNNER = "hackproof-runner:1"
LIMITS = SandboxLimits(
    runner_image=RUNNER,
    app_memory="1g",
    app_cpus=1.0,
    app_pids_limit=256,
    runner_memory="2g",
    startup_timeout_s=60,
    run_timeout_s=600,
)
SPECS = [{"kind": "startup", "claim_type": "startup", "steps": [{"health_path": "/"}]}]
UP = {
    "status": "ok",
    "detail": "HTTP 200 after 1.0s on port 8080",
    "port": 8080,
    "elapsed_ms": 1000,
}
DOWN = {"status": "fail", "detail": "nothing answered on port 8080 within 60 s", "port": None}

Behaviour = Callable[[FakeRuntime, str, ContainerSpec], None]


def stays_up(runtime: FakeRuntime, container_id: str, spec: ContainerSpec) -> None:
    pass


def exits(code: int, logs: str = "", *, oom: bool = False) -> Behaviour:
    def behave(runtime: FakeRuntime, container_id: str, spec: ContainerSpec) -> None:
        runtime.logs[container_id] = logs
        runtime.exit(container_id, code, oom=oom)

    return behave


def logs(text: str) -> Behaviour:
    def behave(runtime: FakeRuntime, container_id: str, spec: ContainerSpec) -> None:
        runtime.logs[container_id] = text

    return behave


def reports(startup: dict[str, object], *, finish: bool = True) -> Behaviour:
    """A runner that checks startup and, with `finish`, writes its results and exits."""

    def behave(runtime: FakeRuntime, container_id: str, spec: ContainerSpec) -> None:
        work = Path(spec.mounts[0][0])
        (work / "startup.json").write_text(json.dumps(startup))
        if finish:
            results = {"runner_version": "1", "startup": startup, "tests": []}
            (work / "results.json").write_text(json.dumps(results))
            runtime.exit(container_id, 0)

    return behave


def crashes(runtime: FakeRuntime, container_id: str, spec: ContainerSpec) -> None:
    runtime.exit(container_id, 1)


def hangs(runtime: FakeRuntime, container_id: str, spec: ContainerSpec) -> None:
    pass


def scripted(apps: list[Behaviour], runners: list[Behaviour]) -> FakeRuntime:
    """A runtime whose n-th app and n-th runner behave as given."""
    runtime = FakeRuntime()
    apps, runners = list(apps), list(runners)

    def on_start(rt: FakeRuntime, container_id: str, spec: ContainerSpec) -> None:
        (apps if spec.alias == "app" else runners).pop(0)(rt, container_id, spec)

    runtime.on_start = on_start
    return runtime


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    path = tmp_path / "work" / str(ATTEMPT) / "run"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def app_log(tmp_path: Path) -> Path:
    return tmp_path / "runs" / str(ATTEMPT) / "app.log"


async def run(
    runtime: FakeRuntime,
    run_dir: Path,
    app_log: Path,
    limits: SandboxLimits = LIMITS,
    mode: str = "final",
) -> RunOutcome:
    # A broken deadline or a missed exit must fail the test, not hang it.
    return await asyncio.wait_for(
        run_sandboxed(
            runtime,
            image=IMAGE,
            attempt_id=ATTEMPT,
            run_dir=run_dir,
            mode=mode,
            specs=SPECS,
            limits=limits,
            app_log_path=app_log,
            poll_s=0.01,
        ),
        timeout=10,
    )


# --- the network and the two containers ----------------------------------------------------------


async def test_the_run_gets_its_own_internal_network(run_dir: Path, app_log: Path) -> None:
    runtime = scripted([stays_up], [reports(UP)])

    await run(runtime, run_dir, app_log)

    assert list(runtime.network_names.values()) == [f"hp-{ATTEMPT}"]
    assert {spec.network for spec in runtime.specs.values()} == {f"hp-{ATTEMPT}"}


async def test_the_app_runs_locked_down_with_three_variables(run_dir: Path, app_log: Path) -> None:
    runtime = scripted([stays_up], [reports(UP)])

    await run(runtime, run_dir, app_log)

    (app,) = runtime.started(app=True)
    assert app == ContainerSpec(
        name=f"hp-{ATTEMPT}-app",
        image=IMAGE,
        network=f"hp-{ATTEMPT}",
        attempt=str(ATTEMPT),
        environment={"PORT": "8080", "HOST": "0.0.0.0", "NODE_ENV": "production"},
        memory="1g",
        cpus=1.0,
        pids_limit=256,
        alias="app",
        user="10001:10001",
        read_only=True,
        mounts=(),
    )


async def test_the_runner_gets_the_run_folder_and_only_the_base_url(
    run_dir: Path, app_log: Path
) -> None:
    runtime = scripted([stays_up], [reports(UP)])

    await run(runtime, run_dir, app_log)

    (runner,) = runtime.started(app=False)
    assert runner == ContainerSpec(
        name=f"hp-{ATTEMPT}-runner",
        image=RUNNER,
        network=f"hp-{ATTEMPT}",
        attempt=str(ATTEMPT),
        environment={"BASE_URL": "http://app:8080"},
        memory="2g",
        cpus=None,
        pids_limit=256,
        alias=None,
        user=None,  # the runner image's own non-root user
        read_only=True,
        mounts=((str(run_dir), "/work"),),
    )


async def test_the_runner_reads_its_request_from_the_run_folder(
    run_dir: Path, app_log: Path
) -> None:
    runtime = scripted([stays_up], [reports(UP)])

    await run(runtime, run_dir, app_log, mode="practice")

    assert json.loads((run_dir / "spec.json").read_text()) == {
        "mode": "practice",
        "base_url": "http://app:8080",
        "candidate_ports": [8080],
        "startup_timeout_s": 60,
        "specs": SPECS,
    }
    assert run_dir.stat().st_mode & 0o777 == 0o777  # the runner's own user writes results here


async def test_ports_the_image_exposes_become_candidates_after_8080(
    run_dir: Path, app_log: Path
) -> None:
    runtime = scripted([stays_up], [reports({**UP, "port": 3000})])
    runtime.exposed[IMAGE] = [3000, 8080, 5000]

    outcome = await run(runtime, run_dir, app_log)

    assert json.loads((run_dir / "spec.json").read_text())["candidate_ports"] == [8080, 3000, 5000]
    assert outcome.flags_used == {
        "root_fallback": False,
        "candidate_ports": [8080, 3000, 5000],
        "port": 3000,
    }


# --- outcomes ------------------------------------------------------------------------------------


async def test_an_app_that_answers_gives_the_runners_results(run_dir: Path, app_log: Path) -> None:
    runtime = scripted([stays_up], [reports(UP)])

    outcome = await run(runtime, run_dir, app_log)

    assert outcome.startup_status == "ok"
    assert outcome.startup_detail == UP["detail"]
    assert outcome.results == {"runner_version": "1", "startup": UP, "tests": []}
    assert outcome.flags_used == {"root_fallback": False, "candidate_ports": [8080], "port": 8080}
    assert outcome.timed_out is False


async def test_an_app_that_exits_before_answering_ends_the_run_at_once(
    run_dir: Path, app_log: Path
) -> None:
    runtime = scripted([exits(1, "ModuleNotFoundError: No module named 'app.main'\n")], [hangs])
    limits = SandboxLimits(**{**LIMITS.__dict__, "run_timeout_s": 3})

    outcome = await run(runtime, run_dir, app_log, limits)

    assert outcome.startup_status == "fail"
    assert outcome.startup_detail == "the app exited with code 1 before answering"
    assert outcome.results is None
    assert "ModuleNotFoundError" in app_log.read_text()


async def test_an_app_killed_for_memory_says_so(run_dir: Path, app_log: Path) -> None:
    runtime = scripted([exits(137, oom=True)], [hangs])

    outcome = await run(runtime, run_dir, app_log)

    assert outcome.startup_detail == "the app exited with code 137 (out of memory) before answering"


async def test_an_app_that_exits_after_answering_is_left_to_the_runner(
    run_dir: Path, app_log: Path
) -> None:
    app_ids: list[str] = []

    def app(rt: FakeRuntime, container_id: str, spec: ContainerSpec) -> None:
        app_ids.append(container_id)

    def runner(rt: FakeRuntime, container_id: str, spec: ContainerSpec) -> None:
        reports(UP)(rt, container_id, spec)
        rt.exit(app_ids[0], 1)  # the app crashes during the tests

    outcome = await run(scripted([app], [runner]), run_dir, app_log)

    assert outcome.startup_status == "ok"
    assert outcome.results is not None


async def test_a_runner_that_writes_no_results_is_an_infrastructure_error(
    run_dir: Path, app_log: Path
) -> None:
    runtime = scripted([stays_up], [crashes])

    with pytest.raises(InfraError, match="runner"):
        await run(runtime, run_dir, app_log)


async def test_a_run_past_its_deadline_is_stopped(run_dir: Path, app_log: Path) -> None:
    runtime = scripted([stays_up], [hangs])
    limits = SandboxLimits(**{**LIMITS.__dict__, "run_timeout_s": 0.2})

    outcome = await run(runtime, run_dir, app_log, limits)

    assert outcome.timed_out is True
    assert outcome.startup_status == "fail"
    assert outcome.startup_detail == "the run timed out after 0.2 s before the app answered"
    assert runtime.containers == {}


async def test_a_run_that_times_out_after_startup_keeps_the_startup_result(
    run_dir: Path, app_log: Path
) -> None:
    runtime = scripted([stays_up], [reports(UP, finish=False)])
    limits = SandboxLimits(**{**LIMITS.__dict__, "run_timeout_s": 0.2})

    outcome = await run(runtime, run_dir, app_log, limits)

    assert outcome.timed_out is True
    assert outcome.startup_status == "ok"
    assert outcome.results is None


# --- fallbacks -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        "Error: EACCES: permission denied, mkdir '/app/data'",
        "OSError: [Errno 30] Read-only file system: '/srv/cache'",
        "EROFS: read-only file system, open 'db.json'",
        "PermissionError: [Errno 13] Permission denied: 'app.db'",
    ],
)
async def test_a_permission_error_restarts_the_app_once_as_the_images_user_and_writable(
    run_dir: Path, app_log: Path, error: str
) -> None:
    runtime = scripted([exits(1, error + "\n"), stays_up], [hangs, reports(UP)])

    outcome = await run(runtime, run_dir, app_log)

    strict, relaxed = runtime.started(app=True)
    assert (strict.user, strict.read_only) == ("10001:10001", True)
    assert (relaxed.user, relaxed.read_only) == (None, False)
    # Every other restriction stays.
    assert relaxed.environment == strict.environment
    assert (relaxed.memory, relaxed.cpus, relaxed.pids_limit) == ("1g", 1.0, 256)
    assert outcome.startup_status == "ok"
    assert outcome.flags_used["root_fallback"] is True


async def test_the_second_pass_starts_from_an_empty_run_folder(
    run_dir: Path, app_log: Path
) -> None:
    seen: list[list[str]] = []

    def runner_after_fallback(rt: FakeRuntime, container_id: str, spec: ContainerSpec) -> None:
        seen.append(sorted(p.name for p in run_dir.iterdir()))
        reports(UP)(rt, container_id, spec)

    runtime = scripted(
        [logs("Error: EACCES: permission denied\n"), stays_up],
        [reports(DOWN), runner_after_fallback],
    )

    await run(runtime, run_dir, app_log)

    assert seen == [["spec.json"]]


async def test_a_startup_failure_without_a_permission_error_is_not_retried(
    run_dir: Path, app_log: Path
) -> None:
    runtime = scripted([logs("listening on 3000\n")], [reports(DOWN)])

    outcome = await run(runtime, run_dir, app_log)

    assert len(runtime.started(app=True)) == 1
    assert outcome.startup_status == "fail"
    assert outcome.flags_used["root_fallback"] is False


async def test_the_permission_fallback_happens_only_once(run_dir: Path, app_log: Path) -> None:
    denied = "permission denied\n"
    runtime = scripted([exits(1, denied), exits(1, denied)], [hangs, hangs])

    outcome = await run(runtime, run_dir, app_log)

    assert len(runtime.started(app=True)) == 2
    assert outcome.startup_status == "fail"
    assert outcome.flags_used["root_fallback"] is True


# --- logs and cleanup ----------------------------------------------------------------------------


async def test_the_last_200_lines_of_the_app_log_are_kept(run_dir: Path, app_log: Path) -> None:
    lines = "".join(f"line {n}\n" for n in range(500))
    runtime = scripted([logs(lines)], [reports(UP)])

    outcome = await run(runtime, run_dir, app_log)

    assert outcome.app_log_path == app_log
    kept = app_log.read_text().splitlines()
    assert kept[0] == "line 300"
    assert kept[-1] == "line 499"
    assert runtime.log_tails == [200]


@pytest.mark.parametrize(
    "runners", [[reports(UP)], [crashes], [hangs]], ids=["finished", "crashed", "timed-out"]
)
async def test_containers_and_network_are_removed_however_the_run_ends(
    run_dir: Path, app_log: Path, runners: list[Behaviour]
) -> None:
    runtime = scripted([stays_up], runners)
    limits = SandboxLimits(**{**LIMITS.__dict__, "run_timeout_s": 0.2})

    with contextlib.suppress(InfraError):  # the crashed runner
        await run(runtime, run_dir, app_log, limits)

    assert runtime.containers == {}
    assert runtime.networks == {}


async def test_cleanup_also_follows_a_container_that_never_started(
    run_dir: Path, app_log: Path
) -> None:
    runtime = scripted([stays_up], [])
    runtime.fail_start.add(RUNNER)

    with pytest.raises(RuntimeError, match="cannot start"):
        await run(runtime, run_dir, app_log)

    assert runtime.containers == {}
    assert runtime.networks == {}
