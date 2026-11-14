"""Liveness and readiness (section 17)."""

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core import worker_health

router = APIRouter(tags=["health"])

DATABASE_TIMEOUT_S = 3


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    """Ready when the database answers and the worker's heartbeat says Docker does (D-030)."""
    state = request.app.state
    database = await _database_answers(state.engine)
    artifact_dir = state.settings.host_artifact_dir
    health = await asyncio.to_thread(worker_health.read, artifact_dir) if artifact_dir else None
    docker = worker_health.docker_ready(health, state.clock.now())
    ready = database and docker
    return JSONResponse(
        {"status": "ready" if ready else "not_ready", "database": database, "docker": docker},
        status_code=200 if ready else 503,
    )


async def _database_answers(engine: AsyncEngine) -> bool:
    try:
        async with asyncio.timeout(DATABASE_TIMEOUT_S), engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        return False
    return True
