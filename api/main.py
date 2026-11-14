"""FastAPI application factory. Run with `uvicorn api.main:create_app --factory`."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.middleware import RequestIdMiddleware
from api.routers import health
from core.clock import Clock, SystemClock
from core.db.engine import make_engine, make_sessions
from core.logging import configure_logging
from core.settings import Settings, get_settings


def create_app(settings: Settings | None = None, *, clock: Clock | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.require_for_api()
    configure_logging(service="api", level=settings.log_level)

    # Creating the engine opens no connection; the first query does.
    engine = make_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await engine.dispose()

    app = FastAPI(
        title="HackProof",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.clock = clock or SystemClock()
    app.state.engine = engine
    app.state.sessions = make_sessions(engine)
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health.router)
    return app
