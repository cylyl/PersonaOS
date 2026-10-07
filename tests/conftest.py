"""pytest configuration + shared fixtures.

v0.1: provides a testcontainers Postgres + async session fixtures for
integration tests. The migration test (Step 1) and the killer acceptance
test (Step 8 — resume after node failure) rely on these.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from personaos.db.models import Base


@pytest.fixture(scope="session")
def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """Session-scoped event loop so async fixtures share a loop."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
def postgres_container():
    """Start a testcontainers Postgres 16 instance for the test session.

    Requires Docker. If unavailable, the test that depends on this
    fixture will skip.
    """
    try:
        from testcontainers.community.postgres import PostgresContainer
    except ImportError as exc:
        pytest.skip(f"testcontainers[postgres] not installed: {exc}")

    container = PostgresContainer("postgres:16-alpine")
    container.start()
    yield container
    container.stop()


@pytest.fixture(scope="session")
def database_url(postgres_container) -> str:
    """Async SQLAlchemy URL for the test container.

    testcontainers returns a sync URL like postgresql://...; convert to
    postgresql+asyncpg:// for SQLAlchemy 2.0 async.
    """
    sync_url = postgres_container.get_connection_url()
    if "+psycopg2" in sync_url:
        return sync_url.replace("postgresql+psycopg2", "postgresql+asyncpg", 1)
    if sync_url.startswith("postgresql://"):
        return sync_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return sync_url


@pytest.fixture
def engine(database_url) -> AsyncEngine:
    """Per-test async engine.

    Per-test (not session-scoped) because asyncpg connection pools retain
    connections in error state after a failed transaction. Using NullPool
    + per-test engines gives each test a fresh connection pool, so a
    failed test can't poison the next one.

    The testcontainers Postgres container is still session-scoped — only
    the SQLAlchemy engine is per-test.
    """
    from sqlalchemy.pool import NullPool

    return create_async_engine(
        database_url, echo=False, future=True, poolclass=NullPool
    )


@pytest.fixture
def session_factory(engine) -> async_sessionmaker[AsyncSession]:
    """Per-test async session factory bound to the per-test engine."""
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@pytest.fixture
async def clean_tables(engine) -> AsyncIterator[None]:
    """Truncate all v0.1 tables before each test that uses this fixture.

    Queries pg_tables first to find existing tables; only truncates those.
    This avoids the Postgres 'transaction in error state' trap that
    happens when TRUNCATE fails on a non-existent table inside a
    transaction block. Order: child tables first (FK constraints).
    """
    async with engine.begin() as conn:
        result = await conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        existing = {row[0] for row in result}
        for table in reversed(Base.metadata.sorted_tables):
            if table.name in existing:
                await conn.execute(text(f"TRUNCATE TABLE {table.name} CASCADE"))
    yield
    await engine.dispose()
