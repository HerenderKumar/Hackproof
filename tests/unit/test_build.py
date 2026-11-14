"""Builds (section 12.2): the method, the commands, the environment, results and log capture.

Commands run through a fake here; tests/integration/test_build.py builds real images.
"""

import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

from core.errors import InfraError
from tests.fakes import FakeCommands, FakeRuntime
from worker.build import (
    BUILD_LOG_LIMIT,
    BuildResult,
    CappedLog,
    build_image,
    build_method,
    image_tag,
    run_logged,
)

TEAM = uuid.UUID("11111111-1111-1111-1111-111111111111")
ATTEMPT = uuid.UUID("22222222-2222-2222-2222-222222222222")
TAG = f"hp/{TEAM}:{ATTEMPT}"
DIGEST = "sha256:" + "ab" * 32
PLAN = {"steps": [{"name": "install"}], "deploy": {"startCommand": "npm run start"}}
INFO = {"detectedProviders": ["node"]}


@pytest.fixture
def src(tmp_path: Path) -> Path:
    app = tmp_path / "work" / "src"
    app.mkdir(parents=True)
    (app / "package.json").write_text('{"scripts": {"start": "node index.js"}}')
    return app


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    return tmp_path / "work" / "build"


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "runs" / str(ATTEMPT) / "build.log"


def railpack_ok() -> FakeCommands:
    commands = FakeCommands()
    commands.on("railpack prepare", output="planning\n", plan=PLAN, info=INFO)
    commands.on("railpack build", output="building\nSuccessfully built image\n")
    return commands


async def build(
    src: Path,
    scratch: Path,
    log_path: Path,
    commands: FakeCommands,
    runtime: FakeRuntime | None = None,
    timeout_s: float = 480,
    buildkit_host: str = "docker-container://buildkit",
) -> BuildResult:
    return await build_image(
        src,
        scratch,
        team_id=TEAM,
        attempt_id=ATTEMPT,
        log_path=log_path,
        runtime=runtime or FakeRuntime(images={TAG: DIGEST}, running={"buildkit"}),
        timeout_s=timeout_s,
        buildkit_host=buildkit_host,
        railpack_version="0.43.0",
        run=commands,
    )


# --- choosing the method -------------------------------------------------------------------------


def test_a_root_dockerfile_means_a_docker_build(src: Path) -> None:
    (src / "Dockerfile").write_text("FROM python:3.12-slim\n")
    assert build_method(src) == "dockerfile"


def test_without_a_root_dockerfile_railpack_builds(src: Path) -> None:
    (src / "deploy").mkdir()
    (src / "deploy" / "Dockerfile").write_text("FROM python:3.12-slim\n")
    assert build_method(src) == "railpack"


def test_a_directory_named_dockerfile_is_not_a_dockerfile(src: Path) -> None:
    (src / "Dockerfile").mkdir()
    assert build_method(src) == "railpack"


def test_the_image_is_named_after_the_team_and_the_attempt() -> None:
    assert image_tag(TEAM, ATTEMPT) == TAG


# --- Railpack builds -----------------------------------------------------------------------------


async def test_railpack_plans_then_builds_the_source(
    src: Path, scratch: Path, log_path: Path
) -> None:
    commands = railpack_ok()

    result = await build(src, scratch, log_path, commands)

    prepare, build_call = commands.calls
    assert prepare.argv[:3] == ["railpack", "prepare", str(src)]
    assert build_call.argv == [
        "railpack", "build", str(src), "--name", TAG, "--progress", "plain",
    ]  # fmt: skip
    assert result == BuildResult(
        status="ok",
        method="railpack",
        exit_code=0,
        image=TAG,
        image_digest=DIGEST,
        build_config={
            "method": "railpack",
            "railpack_version": "0.43.0",
            "plan": PLAN,
            "info": INFO,
        },
        log_path=log_path,
        detail=None,
    )


async def test_the_plan_files_stay_out_of_the_build_context(
    src: Path, scratch: Path, log_path: Path
) -> None:
    commands = railpack_ok()

    await build(src, scratch, log_path, commands)

    prepare = commands.calls[0].argv
    for flag in ("--plan-out", "--info-out"):
        written = Path(prepare[prepare.index(flag) + 1])
        assert not written.is_relative_to(src)
    assert sorted(p.name for p in src.iterdir()) == ["package.json"]


async def test_railpack_gets_buildkit_and_nothing_from_the_worker_environment(
    src: Path, scratch: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("LLM_API_KEY", "GITHUB_TOKEN", "DATABASE_URL", "HOST_ARTIFACT_DIR"):
        monkeypatch.setenv(name, "secret-value")
    commands = railpack_ok()

    await build(src, scratch, log_path, commands)

    for call in commands.calls:
        assert set(call.env) == {"PATH", "HOME", "BUILDKIT_HOST"}
        assert call.env["BUILDKIT_HOST"] == "docker-container://buildkit"
        assert "secret-value" not in call.env.values()


async def test_planning_and_building_share_one_deadline(
    src: Path, scratch: Path, log_path: Path
) -> None:
    commands = railpack_ok()
    before = time.monotonic()

    await build(src, scratch, log_path, commands, timeout_s=300)

    deadlines = {call.deadline for call in commands.calls}
    assert len(deadlines) == 1
    assert before + 300 <= deadlines.pop() <= time.monotonic() + 300


async def test_both_commands_go_into_the_build_log(
    src: Path, scratch: Path, log_path: Path
) -> None:
    await build(src, scratch, log_path, railpack_ok())

    log = log_path.read_text()
    assert "planning" in log
    assert "Successfully built image" in log
    assert log.index("planning") < log.index("Successfully built image")


async def test_a_failed_railpack_build_is_a_result(
    src: Path, scratch: Path, log_path: Path
) -> None:
    commands = FakeCommands()
    commands.on("railpack prepare", plan=PLAN, info=INFO)
    commands.on("railpack build", exit_code=1, output="npm ERR! missing script\n")

    result = await build(src, scratch, log_path, commands)

    assert result.status == "fail"
    assert result.exit_code == 1
    assert result.image is None
    assert result.image_digest is None
    assert result.detail == "railpack build failed (exit 1)"
    assert result.build_config["plan"] == PLAN
    assert "npm ERR! missing script" in log_path.read_text()


async def test_a_plan_railpack_cannot_make_is_a_result_and_nothing_is_built(
    src: Path, scratch: Path, log_path: Path
) -> None:
    commands = FakeCommands()
    commands.on("railpack prepare", exit_code=1, output="no provider matched\n")

    result = await build(src, scratch, log_path, commands)

    assert [c.argv[1] for c in commands.calls] == ["prepare"]
    assert result.status == "fail"
    assert result.exit_code == 1
    assert result.detail == "railpack could not plan the build (exit 1)"
    assert result.build_config == {"method": "railpack", "railpack_version": "0.43.0"}
    assert "no provider matched" in log_path.read_text()


@pytest.mark.parametrize("step", ["railpack prepare", "railpack build"])
async def test_railpack_exit_75_is_an_infrastructure_error(
    src: Path, scratch: Path, log_path: Path, step: str
) -> None:
    commands = railpack_ok()
    commands.on(step, exit_code=75, plan=PLAN, info=INFO)

    with pytest.raises(InfraError, match="75"):
        await build(src, scratch, log_path, commands)


async def test_a_build_that_runs_out_of_time_is_a_failed_build(
    src: Path, scratch: Path, log_path: Path
) -> None:
    commands = FakeCommands()
    commands.on("railpack prepare", plan=PLAN, info=INFO)
    commands.on("railpack build", exit_code=None, output="installing\n")

    result = await build(src, scratch, log_path, commands)

    assert result.status == "fail"
    assert result.exit_code is None
    assert result.detail == "build timed out"
    assert "installing" in log_path.read_text()


async def test_a_failure_while_docker_is_down_is_an_infrastructure_error(
    src: Path, scratch: Path, log_path: Path
) -> None:
    commands = FakeCommands()
    commands.on("railpack prepare", plan=PLAN, info=INFO)
    commands.on("railpack build", exit_code=1, output="cannot connect to the Docker daemon\n")

    with pytest.raises(InfraError, match="Docker"):
        await build(src, scratch, log_path, commands, runtime=FakeRuntime(reachable=False))


async def test_a_railpack_failure_while_buildkit_is_stopped_is_an_infrastructure_error(
    src: Path, scratch: Path, log_path: Path
) -> None:
    # Railpack exits 1 when it cannot reach BuildKit, the same code as a broken project.
    commands = FakeCommands()
    commands.on("railpack prepare", plan=PLAN, info=INFO)
    commands.on("railpack build", exit_code=1, output="failed to connect to buildkit\n")

    with pytest.raises(InfraError, match="BuildKit"):
        await build(src, scratch, log_path, commands, runtime=FakeRuntime(running=set()))


async def test_buildkit_is_only_looked_up_when_it_runs_in_a_container(
    src: Path, scratch: Path, log_path: Path
) -> None:
    commands = FakeCommands()
    commands.on("railpack prepare", plan=PLAN, info=INFO)
    commands.on("railpack build", exit_code=1)

    result = await build(
        src,
        scratch,
        log_path,
        commands,
        runtime=FakeRuntime(running=set()),
        buildkit_host="tcp://buildkit.internal:1234",
    )

    assert result.status == "fail"


async def test_a_successful_build_with_no_image_is_an_infrastructure_error(
    src: Path, scratch: Path, log_path: Path
) -> None:
    with pytest.raises(InfraError, match="no image"):
        await build(src, scratch, log_path, railpack_ok(), runtime=FakeRuntime(images={}))


# --- Dockerfile builds ---------------------------------------------------------------------------


async def test_a_dockerfile_build_labels_the_image_with_the_attempt(
    src: Path, scratch: Path, log_path: Path
) -> None:
    dockerfile = "# syntax=docker/dockerfile:1\nFROM --platform=linux/arm64 node:20 AS deps\n"
    dockerfile += "RUN npm ci\nfrom gcr.io/distroless/nodejs20\nCOPY --from=deps /app /app\n"
    (src / "Dockerfile").write_text(dockerfile)
    commands = FakeCommands()
    commands.on("docker build", output="#1 DONE\n")

    result = await build(src, scratch, log_path, commands)

    (call,) = commands.calls
    assert call.argv == [
        "docker", "build", "-t", TAG, "--label", f"hackproof.attempt={ATTEMPT}",
        "--progress", "plain", str(src),
    ]  # fmt: skip
    assert set(call.env) == {"PATH", "HOME"}
    assert result.status == "ok"
    assert result.method == "dockerfile"
    assert result.image_digest == DIGEST
    assert result.build_config == {
        "method": "dockerfile",
        "dockerfile_sha256": hashlib.sha256(dockerfile.encode()).hexdigest(),
        "base_images": ["node:20", "gcr.io/distroless/nodejs20"],
    }


async def test_a_failed_dockerfile_build_is_a_result(
    src: Path, scratch: Path, log_path: Path
) -> None:
    (src / "Dockerfile").write_text("FROM python:3.12-slim\nRUN exit 3\n")
    commands = FakeCommands()
    commands.on("docker build", exit_code=1, output="did not complete successfully\n")

    result = await build(src, scratch, log_path, commands)

    assert result.status == "fail"
    assert result.exit_code == 1
    assert result.detail == "docker build failed (exit 1)"
    assert result.image is None


async def test_exit_75_from_docker_build_is_an_ordinary_failure(
    src: Path, scratch: Path, log_path: Path
) -> None:
    (src / "Dockerfile").write_text("FROM python:3.12-slim\nRUN exit 75\n")
    commands = FakeCommands()
    commands.on("docker build", exit_code=75)

    result = await build(src, scratch, log_path, commands)

    assert result.status == "fail"
    assert result.exit_code == 75


# --- the build log -------------------------------------------------------------------------------


def test_a_short_log_is_kept_whole(tmp_path: Path) -> None:
    log = CappedLog()
    log.write(b"one\n")
    log.write(b"two\n")
    log.save(tmp_path / "build.log")
    assert (tmp_path / "build.log").read_bytes() == b"one\ntwo\n"


def test_a_long_log_keeps_its_start_and_its_end_within_the_limit(tmp_path: Path) -> None:
    log = CappedLog()
    log.write(b"FIRST LINE\n")
    for _ in range(3 * BUILD_LOG_LIMIT // 1000):
        log.write(b"x" * 999 + b"\n")
    log.write(b"THE ERROR\n")
    log.save(tmp_path / "build.log")

    saved = (tmp_path / "build.log").read_bytes()
    assert len(saved) <= BUILD_LOG_LIMIT
    assert saved.startswith(b"FIRST LINE\n")
    assert saved.endswith(b"THE ERROR\n")
    assert b"bytes of build output left out" in saved


def test_a_log_write_larger_than_the_limit_still_keeps_the_end(tmp_path: Path) -> None:
    log = CappedLog()
    log.write(b"y" * (2 * BUILD_LOG_LIMIT) + b"THE ERROR\n")
    log.save(tmp_path / "build.log")

    saved = (tmp_path / "build.log").read_bytes()
    assert len(saved) <= BUILD_LOG_LIMIT
    assert saved.endswith(b"THE ERROR\n")


# --- running a command ---------------------------------------------------------------------------


async def test_run_logged_returns_the_exit_code_and_captures_both_streams(
    tmp_path: Path,
) -> None:
    log = CappedLog()
    code = await run_logged(
        ["sh", "-c", "echo out; echo err >&2; exit 3"],
        env={"PATH": os.environ["PATH"]},
        deadline=time.monotonic() + 30,
        log=log,
    )
    log.save(tmp_path / "log")

    assert code == 3
    assert (tmp_path / "log").read_text().splitlines() == ["out", "err"]


async def test_run_logged_passes_only_the_given_environment(tmp_path: Path) -> None:
    log = CappedLog()
    await run_logged(
        [sys.executable, "-c", "import os, json; print(json.dumps(sorted(os.environ)))"],
        env={"ONLY": "this"},
        deadline=time.monotonic() + 30,
        log=log,
    )
    log.save(tmp_path / "log")

    names = json.loads((tmp_path / "log").read_text())
    assert [n for n in names if n not in {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}] == ["ONLY"]


async def test_run_logged_kills_the_whole_command_at_the_deadline(tmp_path: Path) -> None:
    child_pid = tmp_path / "child.pid"
    log = CappedLog()
    started = time.monotonic()

    code = await run_logged(
        ["sh", "-c", f"sleep 60 & echo $! > {child_pid}; echo started; wait"],
        env={"PATH": os.environ["PATH"]},
        deadline=started + 1,
        log=log,
    )

    assert code is None
    assert time.monotonic() - started < 10
    # The background child dies with its parent: a killed build leaves nothing running.
    assert_dies(int(child_pid.read_text()))


async def test_run_logged_kills_a_child_left_holding_the_output_open(tmp_path: Path) -> None:
    child_pid = tmp_path / "child.pid"
    started = time.monotonic()

    code = await run_logged(
        ["sh", "-c", f"sleep 60 & echo $! > {child_pid}; exit 0"],
        env={"PATH": os.environ["PATH"]},
        deadline=started + 1,
        log=CappedLog(),
    )

    assert code is None
    assert time.monotonic() - started < 10
    assert_dies(int(child_pid.read_text()))


def assert_dies(pid: int) -> None:
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    pytest.fail(f"process {pid} outlived the deadline")


# --- fresh timestamps (BuildKit's context sync trusts path, size and time, not content) ----------

# 2026-11-01 10:00 UTC in ns: a git archive's commit time. It must lie in the past, or a stamp
# taken from the real clock could not be later than it.
OLD = 1_793_527_200_000_000_000


def stamps(root: Path) -> set[int]:
    found = {root.stat().st_mtime_ns}
    for path in root.rglob("*"):
        found.add(path.lstat().st_mtime_ns)
    return found


def age(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        os.utime(path, ns=(OLD, OLD), follow_symlinks=False)


async def test_every_file_gets_one_fresh_timestamp_before_the_build(
    src: Path, scratch: Path, log_path: Path
) -> None:
    (src / "lib").mkdir()
    (src / "lib" / "util.js").write_text("module.exports = 1\n")
    (src / "link.js").symlink_to("lib/util.js")
    age(src)

    await build(src, scratch, log_path, railpack_ok())

    (stamp,) = stamps(src)
    assert stamp > OLD


async def test_links_are_stamped_themselves_and_never_followed(
    src: Path, scratch: Path, log_path: Path
) -> None:
    outside = src.parent / "outside"
    outside.mkdir()
    (outside / "notes.txt").write_text("not part of the snapshot\n")
    (src / "escape").symlink_to(outside, target_is_directory=True)
    age(src)
    age(outside)

    await build(src, scratch, log_path, railpack_ok())

    assert (src / "escape").lstat().st_mtime_ns > OLD
    assert stamps(outside) == {OLD}


async def test_two_builds_never_share_a_timestamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("worker.build.time.time_ns", lambda: OLD)  # a clock that stands still
    seen = []
    for name in ("a", "b"):
        src = tmp_path / name / "src"
        src.mkdir(parents=True)
        (src / "main.py").write_text("x = 1\n")
        age(src)  # both start from the same commit time
        await build(src, tmp_path / name / "build", tmp_path / name / "build.log", railpack_ok())
        seen.append(stamps(src).pop())

    assert seen[0] != seen[1]
