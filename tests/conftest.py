"""Shared fixtures. Database tests run against a throwaway `*_test` database (D-031)."""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from tests.api_helpers import Api, make_app
from tests.factories import T0
from tests.fakes import FakeClock, FakeRepoProvider

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://hp:hp@127.0.0.1:54329/hp_test"
)


async def _recreate_database(url: str) -> None:
    target = make_url(url)
    name = target.database or ""
    # The only place in the codebase that drops a database; never point it at real data.
    if not name.endswith("_test"):
        raise RuntimeError(f"refusing to recreate database {name!r}: name must end with _test")
    admin = create_async_engine(
        target.set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    async with admin.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    await admin.dispose()


@pytest.fixture(scope="session")
def migrated_database_url() -> str:
    """A freshly created and migrated test database. Sync, so Alembic can own its event loop."""
    asyncio.run(_recreate_database(TEST_DATABASE_URL))
    config = Config("alembic.ini")
    config.cmd_opts = None
    config.attributes["url_override"] = TEST_DATABASE_URL
    command.upgrade(config, "head")
    return TEST_DATABASE_URL


@pytest.fixture(scope="session")
async def engine(migrated_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(migrated_database_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
async def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """A session factory over an emptied database."""
    async with engine.begin() as conn:
        tables = (
            await conn.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
                )
            )
        ).scalars()
        names = ", ".join(f'"{t}"' for t in tables)
        if names:
            await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    return async_sessionmaker(engine, expire_on_commit=False)


# --- the API, with its clock and repository provider under the test's control ---------------


@pytest.fixture
def clock() -> FakeClock:
    """Six hours before the kickoff of the events the tests create.

    Close enough that a session started now (12 hours) is still alive just after kickoff.
    """
    return FakeClock(T0 - timedelta(hours=6))


@pytest.fixture
def repos() -> FakeRepoProvider:
    return FakeRepoProvider()


@pytest.fixture
async def api(
    sessions: async_sessionmaker[AsyncSession],
    migrated_database_url: str,
    tmp_path: Path,
    clock: FakeClock,
    repos: FakeRepoProvider,
) -> AsyncIterator[Api]:
    app = make_app(migrated_database_url, tmp_path, clock, repos)
    helper = Api(app)
    yield helper
    await helper.aclose()
    await app.state.engine.dispose()
