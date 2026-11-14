from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from api.main import create_app
from core import worker_health
from core.settings import Settings
from core.worker_health import WorkerHealth
from tests.fakes import FakeClock

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
UNREACHABLE_DATABASE = "postgresql+asyncpg://hp:hp@127.0.0.1:1/hp"


@asynccontextmanager
async def app_client(**overrides: Any) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        organiser_password="pw",
        session_secret="s" * 32,
        **{"database_url": UNREACHABLE_DATABASE, **overrides},
    )
    app = create_app(settings, clock=FakeClock(NOW))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client
    await app.state.engine.dispose()


async def test_healthz_reports_the_process_is_up() -> None:
    async with app_client() as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_every_response_carries_a_request_id() -> None:
    async with app_client() as client:
        response = await client.get("/healthz")
    assert len(response.headers["x-request-id"]) >= 16


async def test_an_incoming_request_id_is_echoed() -> None:
    async with app_client() as client:
        response = await client.get("/healthz", headers={"x-request-id": "abc-123-abc-123-abc"})
    assert response.headers["x-request-id"] == "abc-123-abc-123-abc"


# --- /readyz: the database answers and the worker's heartbeat says Docker does too (D-030) ---


async def test_readyz_is_ready_when_the_database_and_docker_answer(
    migrated_database_url: str, tmp_path: Path
) -> None:
    worker_health.write(tmp_path, WorkerHealth(at=NOW - timedelta(seconds=10), docker_ok=True))

    async with app_client(database_url=migrated_database_url, host_artifact_dir=tmp_path) as c:
        response = await c.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": True, "docker": True}


async def test_readyz_fails_when_the_worker_has_never_reported(
    migrated_database_url: str, tmp_path: Path
) -> None:
    async with app_client(database_url=migrated_database_url, host_artifact_dir=tmp_path) as c:
        response = await c.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "database": True, "docker": False}


async def test_readyz_fails_when_the_worker_reports_docker_down(
    migrated_database_url: str, tmp_path: Path
) -> None:
    worker_health.write(tmp_path, WorkerHealth(at=NOW, docker_ok=False))

    async with app_client(database_url=migrated_database_url, host_artifact_dir=tmp_path) as c:
        response = await c.get("/readyz")

    assert response.status_code == 503
    assert response.json()["docker"] is False


async def test_readyz_fails_when_the_heartbeat_is_stale(
    migrated_database_url: str, tmp_path: Path
) -> None:
    worker_health.write(tmp_path, WorkerHealth(at=NOW - timedelta(minutes=5), docker_ok=True))

    async with app_client(database_url=migrated_database_url, host_artifact_dir=tmp_path) as c:
        response = await c.get("/readyz")

    assert response.status_code == 503


async def test_readyz_fails_when_the_database_is_unreachable(tmp_path: Path) -> None:
    worker_health.write(tmp_path, WorkerHealth(at=NOW, docker_ok=True))

    async with app_client(database_url=UNREACHABLE_DATABASE, host_artifact_dir=tmp_path) as c:
        response = await c.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "database": False, "docker": True}


async def test_readyz_fails_without_an_artifact_directory(migrated_database_url: str) -> None:
    async with app_client(database_url=migrated_database_url, host_artifact_dir=None) as c:
        response = await c.get("/readyz")

    assert response.status_code == 503
    assert response.json()["docker"] is False
