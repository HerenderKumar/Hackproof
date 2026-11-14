"""FastAPI application factory. Run with `uvicorn api.main:create_app --factory`."""

from fastapi import FastAPI

from api.middleware import RequestIdMiddleware
from api.routers import health
from core.logging import configure_logging
from core.settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.require_for_api()
    configure_logging(service="api", level=settings.log_level)

    app = FastAPI(title="HackProof", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.settings = settings
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health.router)
    return app
