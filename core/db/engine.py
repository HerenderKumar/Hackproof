"""Async engine and session factory."""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

Sessions = async_sessionmaker[AsyncSession]


def make_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, pool_pre_ping=True)


def make_sessions(engine: AsyncEngine) -> Sessions:
    # Rows stay readable after commit: async code cannot lazy-load expired attributes.
    return async_sessionmaker(engine, expire_on_commit=False)
