"""Real builds through Railpack and `docker build`. Runs inside the worker image."""

import contextlib
import shutil
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import docker
import docker.errors
import pytest

from scripts.fixtures import FIXTURES_DIR
from worker.build import BuildResult, build_image, image_tag
from worker.runtime import ATTEMPT_LABEL, DockerRuntime

pytestmark = pytest.mark.integration

TEAM = uuid.UUID("33333333-3333-3333-3333-333333333333")


@pytest.fixture
def attempt() -> Iterator[uuid.UUID]:
    """A fresh attempt ID; its image is removed afterwards."""
    attempt_id = uuid.uuid4()
    yield attempt_id
    client = docker.from_env()
    with contextlib.suppress(docker.errors.ImageNotFound):
        client.images.remove(image_tag(TEAM, attempt_id), force=True)
    client.close()


def copy_app(fixture: str, variant: str, dest: Path) -> Path:
    src = dest / "work" / "src"
    shutil.copytree(FIXTURES_DIR / fixture / variant, src)
    return src


async def build(src: Path, attempt: uuid.UUID, timeout_s: float = 480) -> BuildResult:
    return await build_image(
        src,
        src.parent / "build",
        team_id=TEAM,
        attempt_id=attempt,
        log_path=src.parent.parent / "runs" / str(attempt) / "build.log",
        runtime=DockerRuntime(),
        timeout_s=timeout_s,
        buildkit_host="docker-container://buildkit",
        railpack_version="0.43.0",
    )


def write_app(dest: Path, files: dict[str, str]) -> Path:
    src = dest / "work" / "src"
    src.mkdir(parents=True)
    for name, content in files.items():
        (src / name).write_text(content)
    return src


async def test_railpack_builds_a_node_app_into_docker(tmp_path: Path, attempt: uuid.UUID) -> None:
    src = copy_app("F01-node-splitter", "good", tmp_path)

    result = await build(src, attempt)

    assert result.status == "ok", result.log_path.read_text()[-2000:]
    assert result.method == "railpack"
    client = docker.from_env()
    assert client.images.get(image_tag(TEAM, attempt)).id == result.image_digest
    assert result.build_config["plan"]["deploy"]["startCommand"]
    assert "Successfully built image" in result.log_path.read_text()


async def test_a_root_dockerfile_is_built_with_docker_and_labelled(
    tmp_path: Path, attempt: uuid.UUID
) -> None:
    src = copy_app("F08-dockerfile-temperature", "good", tmp_path)

    result = await build(src, attempt)

    assert result.status == "ok", result.log_path.read_text()[-2000:]
    assert result.method == "dockerfile"
    assert result.build_config["base_images"] == ["python:3.12-slim"]
    image = docker.from_env().images.get(image_tag(TEAM, attempt))
    assert image.id == result.image_digest
    assert image.labels[ATTEMPT_LABEL] == str(attempt)


async def test_a_failing_dockerfile_step_is_a_failed_build(
    tmp_path: Path, attempt: uuid.UUID
) -> None:
    src = write_app(tmp_path, {"Dockerfile": "FROM python:3.12-slim\nRUN echo boom && exit 3\n"})

    result = await build(src, attempt)

    assert result.status == "fail"
    assert result.exit_code not in (0, None)
    assert result.image_digest is None
    assert "boom" in result.log_path.read_text()


async def test_a_project_railpack_cannot_build_is_a_failed_build(
    tmp_path: Path, attempt: uuid.UUID
) -> None:
    src = write_app(tmp_path, {"notes.txt": "nothing to run here\n"})

    result = await build(src, attempt)

    assert result.status == "fail"
    assert result.exit_code not in (0, 75, None)
    assert result.log_path.read_text()


async def test_a_build_past_its_deadline_is_stopped(tmp_path: Path, attempt: uuid.UUID) -> None:
    src = write_app(tmp_path, {"Dockerfile": "FROM python:3.12-slim\nRUN sleep 300\n"})
    started = time.monotonic()

    result = await build(src, attempt, timeout_s=15)

    assert result.status == "fail"
    assert result.detail == "build timed out"
    assert time.monotonic() - started < 60


async def test_image_id_is_none_for_a_missing_image() -> None:
    assert await DockerRuntime().image_id(f"hp/{uuid.uuid4()}:missing") is None


async def test_is_running_finds_the_buildkit_container_by_name() -> None:
    runtime = DockerRuntime()
    assert await runtime.is_running("buildkit") is True
    assert await runtime.is_running(f"no-such-container-{uuid.uuid4()}") is False
