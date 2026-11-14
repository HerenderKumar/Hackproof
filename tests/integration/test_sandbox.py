"""Runs against the real Docker daemon (section 12.3). Runs inside the worker image.

The three acceptance tests section 12.3 asks for: an app on a run network cannot reach the
internet; an app in run A cannot reach the app in run B; neither container's environment holds
anything from the worker's. Each probe has a positive control, so a probe that can never succeed
cannot pass by accident. The containers come from the sandbox's own spec builders; only the
command is replaced with the probe.

Run folders live under HOST_ARTIFACT_DIR: the daemon resolves bind-mount paths on the host.
"""

import asyncio
import contextlib
import dataclasses
import shutil
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import docker
import docker.errors
import pytest

from core.settings import Settings
from scripts.fixtures import FIXTURES_DIR
from worker.build import build_image, image_tag
from worker.runtime import ATTEMPT_LABEL, ContainerSpec, DockerRuntime
from worker.sandbox import (
    RunOutcome,
    SandboxLimits,
    app_container,
    network_name,
    run_sandboxed,
    runner_container,
)

pytestmark = pytest.mark.integration

PYTHON = "python:3.12-slim"
TEAM = uuid.UUID("55555555-5555-5555-5555-555555555555")
STARTUP_SPEC = {
    "kind": "startup",
    "claim_type": "startup",
    "steps": [{"health_path": "/"}],
    "practice_cases": [],
    "final_cases": [],
}

# Exit 0 if the URL answers, 3 if it does not.
REACH = """
import sys, urllib.request
try:
    urllib.request.urlopen(sys.argv[1], timeout=4)
except Exception as exc:
    print("blocked:", type(exc).__name__, exc)
    sys.exit(3)
print("reached")
"""

# Exit 0 if anything outside answers (by name or by address), 3 if nothing does.
INTERNET = """
import socket, sys, urllib.request
for attempt in (
    lambda: urllib.request.urlopen("https://example.com", timeout=4),
    lambda: socket.create_connection(("1.1.1.1", 443), timeout=4),
):
    try:
        attempt()
    except Exception as exc:
        print("blocked:", type(exc).__name__, exc)
    else:
        print("reached")
        sys.exit(0)
sys.exit(3)
"""


@dataclasses.dataclass
class Docker:
    runtime: DockerRuntime
    client: docker.DockerClient
    limits: SandboxLimits
    artifacts: Path
    attempts: list[uuid.UUID]

    def attempt(self) -> uuid.UUID:
        attempt_id = uuid.uuid4()
        self.attempts.append(attempt_id)
        return attempt_id

    def run_dir(self, attempt_id: uuid.UUID) -> Path:
        path = self.artifacts / "work" / str(attempt_id) / "run"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def labelled(self, attempt_id: uuid.UUID) -> tuple[list[object], list[object]]:
        label = {"label": f"{ATTEMPT_LABEL}={attempt_id}"}
        return (
            self.client.containers.list(all=True, filters=label),
            self.client.networks.list(filters=label),
        )

    async def start(self, spec: ContainerSpec, *command: str) -> str:
        return await self.runtime.start_container(dataclasses.replace(spec, command=command))

    async def exit_code(self, container_id: str, within_s: float = 60) -> int | None:
        for _ in range(int(within_s * 4)):
            state = await self.runtime.container_state(container_id)
            if not state.running:
                return state.exit_code
            await asyncio.sleep(0.25)
        return None

    async def logs(self, container_id: str) -> str:
        return await self.runtime.container_logs(container_id, 50)


@pytest.fixture
async def dk() -> AsyncIterator[Docker]:
    settings = Settings()
    client = docker.from_env()
    harness = Docker(
        runtime=DockerRuntime(),
        client=client,
        limits=SandboxLimits.from_settings(settings),
        artifacts=settings.artifact_dir / "integration",
        attempts=[],
    )
    try:
        yield harness
    finally:
        for attempt_id in harness.attempts:
            containers, networks = harness.labelled(attempt_id)
            for container in containers:
                with contextlib.suppress(docker.errors.NotFound):
                    container.remove(force=True)  # type: ignore[attr-defined]
            for network in networks:
                with contextlib.suppress(docker.errors.NotFound):
                    network.remove()  # type: ignore[attr-defined]
            with contextlib.suppress(docker.errors.ImageNotFound):
                client.images.remove(image_tag(TEAM, attempt_id), force=True)
        shutil.rmtree(harness.artifacts, ignore_errors=True)
        client.close()


@pytest.fixture
def open_network(dk: Docker) -> Iterator[str]:
    """An ordinary network with a route out, for the positive control."""
    attempt_id = dk.attempt()
    name = network_name(attempt_id)
    dk.client.networks.create(name, labels={ATTEMPT_LABEL: str(attempt_id)})
    yield name


# --- section 12.3's acceptance tests -------------------------------------------------------------


async def test_an_app_on_a_run_network_cannot_reach_the_internet(
    dk: Docker, open_network: str
) -> None:
    attempt_id = dk.attempt()
    await dk.runtime.create_network(network_name(attempt_id), str(attempt_id))
    app = app_container(PYTHON, attempt_id, dk.limits)

    sealed = await dk.start(app, "python", "-c", INTERNET)
    control = await dk.start(
        dataclasses.replace(app, name=f"{app.name}-control", network=open_network),
        "python", "-c", INTERNET,
    )  # fmt: skip

    assert await dk.exit_code(control) == 0, await dk.logs(control)
    assert await dk.exit_code(sealed) == 3, await dk.logs(sealed)


async def test_nothing_in_one_run_can_reach_the_app_of_another(dk: Docker) -> None:
    run_a, run_b = dk.attempt(), dk.attempt()
    for attempt_id in (run_a, run_b):
        await dk.runtime.create_network(network_name(attempt_id), str(attempt_id))
    app_b = await dk.start(
        app_container(PYTHON, run_b, dk.limits), "python", "-m", "http.server", "8080"
    )
    address_b = dk.client.containers.get(app_b).attrs["NetworkSettings"]["Networks"][
        network_name(run_b)
    ]["IPAddress"]

    # Positive control: inside run B, B's app answers by alias and by address.
    runner_b = runner_container(run_b, dk.run_dir(run_b), dk.limits)
    for n, url in enumerate(("http://app:8080/", f"http://{address_b}:8080/")):
        control = await dk.start(
            dataclasses.replace(runner_b, name=f"{runner_b.name}-control-{n}"),
            "python", "-c", REACH, url,
        )  # fmt: skip
        assert await dk.exit_code(control) == 0, await dk.logs(control)

    # From run A, neither its app nor its runner reaches B, by address or by container name.
    targets = (f"http://{address_b}:8080/", f"http://{network_name(run_b)}-app:8080/")
    a_specs = [
        app_container(PYTHON, run_a, dk.limits),
        runner_container(run_a, dk.run_dir(run_a), dk.limits),
    ]
    for spec in a_specs:
        for n, url in enumerate(targets):
            probe = await dk.start(
                dataclasses.replace(spec, name=f"{spec.name}-{n}"), "python", "-c", REACH, url
            )
            assert await dk.exit_code(probe) == 3, f"{spec.name} reached {url}"


@pytest.mark.parametrize("which", ["app", "runner"])
async def test_no_container_inherits_anything_from_the_worker_environment(
    dk: Docker, which: str
) -> None:
    attempt_id = dk.attempt()
    await dk.runtime.create_network(network_name(attempt_id), str(attempt_id))
    if which == "app":
        spec = app_container(PYTHON, attempt_id, dk.limits)
    else:
        spec = runner_container(attempt_id, dk.run_dir(attempt_id), dk.limits)

    container_id = await dk.start(spec, "sleep", "30")

    def as_dict(env: list[str] | None) -> dict[str, str]:
        return dict(item.split("=", 1) for item in env or [])

    image_env = as_dict(dk.client.images.get(spec.image).attrs["Config"]["Env"])
    container_env = as_dict(dk.client.containers.get(container_id).attrs["Config"]["Env"])
    assert container_env == {**image_env, **spec.environment}
    for secret in ("DATABASE_URL", "HOST_ARTIFACT_DIR", "LLM_API_KEY", "GITHUB_TOKEN"):
        assert secret not in container_env


# --- whole runs ----------------------------------------------------------------------------------


async def build_and_run(dk: Docker, src: Path) -> RunOutcome:
    attempt_id = dk.attempt()
    work = dk.artifacts / "work" / str(attempt_id)
    built = await build_image(
        src,
        work / "build",
        team_id=TEAM,
        attempt_id=attempt_id,
        log_path=work / "build.log",
        runtime=dk.runtime,
        timeout_s=300,
        buildkit_host="docker-container://buildkit",
        railpack_version="0.43.0",
    )
    assert built.status == "ok", built.log_path.read_text()[-2000:]
    assert built.image is not None
    outcome = await run_sandboxed(
        dk.runtime,
        image=built.image,
        attempt_id=attempt_id,
        run_dir=dk.run_dir(attempt_id),
        mode="final",
        specs=[STARTUP_SPEC],
        limits=dk.limits,
        app_log_path=work / "app.log",
    )
    # Whatever the outcome, nothing of the run is left behind (section 12.3, step 6).
    assert dk.labelled(attempt_id) == ([], [])
    return outcome


def source(tmp_path: Path, dockerfile: str, **files: str) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "Dockerfile").write_text(dockerfile)
    for name, content in files.items():
        (src / name).write_text(content)
    return src


async def test_a_good_app_starts_and_the_runner_reports_it(dk: Docker, tmp_path: Path) -> None:
    src = tmp_path / "src"
    shutil.copytree(FIXTURES_DIR / "F08-dockerfile-temperature" / "good", src)

    outcome = await build_and_run(dk, src)

    assert outcome.startup_status == "ok", outcome.startup_detail
    assert outcome.flags_used == {"root_fallback": False, "candidate_ports": [8080], "port": 8080}
    assert outcome.results is not None
    assert outcome.results["startup"]["status"] == "ok"


async def test_an_app_that_crashes_at_start_fails_at_once(dk: Docker, tmp_path: Path) -> None:
    src = tmp_path / "src"
    shutil.copytree(FIXTURES_DIR / "F08-dockerfile-temperature" / "broken", src)

    outcome = await build_and_run(dk, src)

    assert outcome.startup_status == "fail"
    assert outcome.startup_detail == "the app exited with code 1 before answering"
    assert "No module named" in outcome.app_log_path.read_text()


async def test_an_app_that_writes_to_its_folder_gets_the_permission_fallback(
    dk: Docker, tmp_path: Path
) -> None:
    app = (
        "import http.server, os\n"
        "open('state.txt', 'w').write('started')\n"
        "server = http.server.HTTPServer(('', int(os.environ['PORT'])),"
        " http.server.SimpleHTTPRequestHandler)\n"
        "server.serve_forever()\n"
    )
    dockerfile = f'FROM {PYTHON}\nWORKDIR /srv\nCOPY app.py .\nCMD ["python", "app.py"]\n'

    outcome = await build_and_run(dk, source(tmp_path, dockerfile, **{"app.py": app}))

    assert outcome.startup_status == "ok", outcome.startup_detail
    assert outcome.flags_used["root_fallback"] is True


async def test_an_app_on_its_own_port_is_found_through_the_ports_it_exposes(
    dk: Docker, tmp_path: Path
) -> None:
    dockerfile = f'FROM {PYTHON}\nEXPOSE 3000\nCMD ["python", "-m", "http.server", "3000"]\n'

    outcome = await build_and_run(dk, source(tmp_path, dockerfile))

    assert outcome.startup_status == "ok", outcome.startup_detail
    assert outcome.flags_used == {
        "root_fallback": False,
        "candidate_ports": [8080, 3000],
        "port": 3000,
    }
