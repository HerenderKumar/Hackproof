"""Alembic environment. Migrations are literal SQL from section 10 of the brief (D-028)."""

import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from core.settings import get_settings


def _url() -> str:
    """Tests pass a URL through `config.attributes`; everything else uses DATABASE_URL."""
    override = context.config.attributes.get("url_override")
    return str(override) if override else get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(url=_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_url(), poolclass=NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


def _run(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
