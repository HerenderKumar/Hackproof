"""Builds (section 12.2): turn an extracted snapshot into an image named `hp/<team>:<attempt>`.

A `Dockerfile` at the project root is built with `docker build`; anything else with Railpack,
which plans first (`railpack prepare`, whose plan is kept as the build config) and then builds
through the BuildKit container. Planning and building share one `BUILD_TIMEOUT_S` deadline.

How a build ends:
- it worked: the result carries the image tag and digest;
- the team's build failed, or ran out of time: a `fail` result (tests become `blocked`);
- something outside the team's code went wrong (Railpack exit 75, Docker unreachable, an image
  that never reached Docker): `InfraError`, so the job is retried (section 11.3).

Builds run the team's instructions, so their commands get a bare environment: `PATH`, `HOME`
and, for Railpack, `BUILDKIT_HOST`. Nothing else from the worker reaches them.
"""

import asyncio
import contextlib
import hashlib
import json
import os
import re
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID

from core.errors import InfraError
from worker.runtime import ATTEMPT_LABEL, ContainerRuntime

BuildMethod = Literal["railpack", "dockerfile"]

# Section 12.2 caps the saved log at 1 MB. The start says what Railpack detected and the end
# says why a build failed, so a longer log keeps both and drops the middle.
BUILD_LOG_LIMIT = 1 << 20
_LOG_HEAD = 64 << 10
_NOTE = "\n[... {} bytes of build output left out ...]\n"
_NOTE_ROOM = 128

RAILPACK_TRANSIENT_EXIT = 75
_DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_FROM = re.compile(r"^[ \t]*FROM[ \t]+(?:--\S+[ \t]+)*(\S+)", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class BuildResult:
    status: Literal["ok", "fail"]
    method: BuildMethod
    exit_code: int | None  # None when the build was killed at its deadline
    image: str | None  # set only when the build worked
    image_digest: str | None
    build_config: dict[str, Any]
    log_path: Path
    detail: str | None  # why a build failed, in words a team can act on


class CappedLog:
    """Command output held in memory up to `limit` bytes: the start and the end of it."""

    def __init__(self, limit: int = BUILD_LOG_LIMIT, head: int = _LOG_HEAD) -> None:
        self._head_limit = head
        self._tail_limit = limit - head - _NOTE_ROOM
        self._head = bytearray()
        self._tail = bytearray()
        self._tail_written = 0

    def write(self, data: bytes) -> None:
        room = self._head_limit - len(self._head)
        if room > 0:
            self._head += data[:room]
            data = data[room:]
        self._tail += data
        self._tail_written += len(data)
        if len(self._tail) > 2 * self._tail_limit:  # trim now and then, not on every write
            del self._tail[: -self._tail_limit]

    def save(self, path: Path) -> None:
        tail = self._tail[-self._tail_limit :] if self._tail_limit else b""
        dropped = self._tail_written - len(tail)
        note = _NOTE.format(dropped).encode() if dropped else b""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes(self._head) + note + bytes(tail))


class CommandRunner(Protocol):
    """Runs a command to completion. Returns its exit code, or None if it hit the deadline."""

    async def __call__(
        self, argv: list[str], *, env: dict[str, str], deadline: float, log: CappedLog
    ) -> int | None: ...


async def run_logged(
    argv: list[str], *, env: dict[str, str], deadline: float, log: CappedLog
) -> int | None:
    """Run `argv` with exactly `env`, both output streams into `log`, until `deadline`
    (a `time.monotonic()` value). At the deadline the command and everything it started
    are killed, and the result is None."""
    process = await asyncio.create_subprocess_exec(
        *argv,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,  # its own process group, so one signal reaches every child
    )

    async def finish() -> int:
        assert process.stdout is not None
        while chunk := await process.stdout.read(1 << 16):
            log.write(chunk)
        return await process.wait()

    finished = False
    try:
        code = await asyncio.wait_for(finish(), max(0.0, deadline - time.monotonic()))
        finished = True
        return code
    except TimeoutError:
        return None
    finally:
        # Timed out or cancelled. Kill the group even if its leader has exited: a child that
        # still holds the output pipe open is what kept us waiting.
        if not finished:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


def build_method(src: Path) -> BuildMethod:
    return "dockerfile" if (src / "Dockerfile").is_file() else "railpack"


def image_tag(team_id: UUID, attempt_id: UUID) -> str:
    return f"hp/{team_id}:{attempt_id}"


async def build_image(
    src: Path,
    scratch: Path,
    *,
    team_id: UUID,
    attempt_id: UUID,
    log_path: Path,
    runtime: ContainerRuntime,
    timeout_s: float,
    buildkit_host: str,
    railpack_version: str,
    run: CommandRunner = run_logged,
) -> BuildResult:
    """Build `src` (an extracted snapshot). `scratch` is a directory outside `src` for files
    the build makes but must not see; the log is saved at `log_path` however the build ends."""
    deadline = time.monotonic() + timeout_s
    tag = image_tag(team_id, attempt_id)
    log = CappedLog()
    method = await asyncio.to_thread(build_method, src)
    try:
        if method == "dockerfile":
            config = await asyncio.to_thread(_dockerfile_info, src)
            argv = [
                "docker", "build", "-t", tag, "--label", f"{ATTEMPT_LABEL}={attempt_id}",
                "--progress", "plain", str(src),
            ]  # fmt: skip
            code = await run(argv, env=_bare_env(), deadline=deadline, log=log)
            failure = _failure(code, f"docker build failed (exit {code})")
        else:
            config, code, failure = await _railpack(
                src, scratch, tag, buildkit_host, railpack_version, deadline, log, run
            )
    finally:
        await asyncio.to_thread(log.save, log_path)

    if failure is not None:
        if not await runtime.ping():
            raise InfraError("the build failed while the Docker daemon was unreachable")
        # Railpack exits 1 both for a broken project and for a BuildKit it cannot reach.
        buildkit = _buildkit_container(buildkit_host)
        if method == "railpack" and buildkit and not await runtime.is_running(buildkit):
            raise InfraError(f"the build failed while BuildKit ({buildkit}) was not running")
        return BuildResult("fail", method, code, None, None, config, log_path, failure)

    digest = await runtime.image_id(tag)
    if digest is None:
        raise InfraError(f"the build reported success but Docker has no image {tag}")
    return BuildResult("ok", method, code, tag, digest, config, log_path, None)


# --- helpers -------------------------------------------------------------------------------------


async def _railpack(
    src: Path,
    scratch: Path,
    tag: str,
    buildkit_host: str,
    railpack_version: str,
    deadline: float,
    log: CappedLog,
    run: CommandRunner,
) -> tuple[dict[str, Any], int | None, str | None]:
    """Plan, then build. Returns the build config, the last exit code and any failure."""
    config: dict[str, Any] = {"method": "railpack", "railpack_version": railpack_version}
    env = {**_bare_env(), "BUILDKIT_HOST": buildkit_host}
    plan_out, info_out = scratch / "plan.json", scratch / "info.json"
    await asyncio.to_thread(scratch.mkdir, parents=True, exist_ok=True)

    argv = [
        "railpack", "prepare", str(src), "--plan-out", str(plan_out), "--info-out", str(info_out),
    ]  # fmt: skip
    code = await run(argv, env=env, deadline=deadline, log=log)
    _raise_if_transient(code)
    if code != 0:
        return config, code, _failure(code, f"railpack could not plan the build (exit {code})")
    config["plan"] = await asyncio.to_thread(_read_json, plan_out)
    config["info"] = await asyncio.to_thread(_read_json, info_out)

    code = await run(
        ["railpack", "build", str(src), "--name", tag, "--progress", "plain"],
        env=env,
        deadline=deadline,
        log=log,
    )
    _raise_if_transient(code)
    return config, code, _failure(code, f"railpack build failed (exit {code})")


def _raise_if_transient(code: int | None) -> None:
    if code == RAILPACK_TRANSIENT_EXIT:
        raise InfraError(f"railpack exited {code} (transient), retrying")


def _failure(code: int | None, message: str) -> str | None:
    if code is None:
        return "build timed out"
    return None if code == 0 else message


def _buildkit_container(buildkit_host: str) -> str | None:
    """The container name in `docker-container://<name>`, else None (BuildKit runs elsewhere)."""
    scheme = "docker-container://"
    if not buildkit_host.startswith(scheme):
        return None
    return buildkit_host.removeprefix(scheme) or None


def _bare_env() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", _DEFAULT_PATH), "HOME": os.environ.get("HOME", "/root")}


def _dockerfile_info(src: Path) -> dict[str, Any]:
    content = (src / "Dockerfile").read_bytes()
    return {
        "method": "dockerfile",
        "dockerfile_sha256": hashlib.sha256(content).hexdigest(),
        "base_images": _FROM.findall(content.decode(errors="replace")),
    }


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text())
