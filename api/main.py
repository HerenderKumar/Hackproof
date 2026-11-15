"""FastAPI application factory. Run with `uvicorn api.main:create_app --factory`."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.auth import SessionCodec
from api.middleware import RequestIdMiddleware
from api.routers import auth, events, health, team, team_claims, team_tests
from core.clock import Clock, SystemClock
from core.db.engine import make_engine, make_sessions
from core.logging import configure_logging
from core.providers import make_provider
from core.repo import RepoProvider
from core.settings import Settings, get_settings


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    repo_provider: RepoProvider | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    settings.require_for_api()
    configure_logging(service="api", level=settings.log_level)
    clock = clock or SystemClock()
    provider = repo_provider or make_provider(settings, clock)

    # Creating the engine opens no connection; the first query does.
    engine = make_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await engine.dispose()
        close = getattr(provider, "aclose", None)
        if close is not None:
            await close()

    # The interactive docs list every route, so they are served in local development only.
    app = FastAPI(
        title="HackProof",
        docs_url="/api/docs" if settings.is_local else None,
        openapi_url="/api/openapi.json" if settings.is_local else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    assert settings.session_secret is not None  # checked by require_for_api
    app.state.settings = settings
    app.state.clock = clock
    app.state.engine = engine
    app.state.sessions = make_sessions(engine)
    app.state.codec = SessionCodec(settings.session_secret.get_secret_value())
    app.state.repo_provider = provider
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(events.router)
    app.include_router(team.router)
    app.include_router(team_claims.router)
    app.include_router(team_tests.router)
    return app
