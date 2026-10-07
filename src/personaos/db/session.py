"""Async PostgreSQL engine and session factory.

v0.1 uses SQLAlchemy 2.0 async with asyncpg.
Engine + sessionmaker are instantiated once per process (in
api/app.py startup or worker_main.py) and reused.

Usage:
    from personaos.config import settings
    from personaos.db.session import make_engine, make_session_factory

    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)

    async with session_factory() as session:
        result = await session.execute(select(Worker))
        workers = result.scalars().all()
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def make_engine(
    database_url: str,
    *,
    echo: bool = False,
    pool_size: int = 5,
    max_overflow: int = 10,
    pool_pre_ping: bool = True,
) -> AsyncEngine:
    """Create an async SQLAlchemy engine.

    Defaults:
      - pool_size=5 (overridable via DATABASE_POOL_SIZE)
      - max_overflow=10 (overridable via DATABASE_MAX_OVERFLOW)
      - pool_pre_ping=True (verify connection liveness)
    """
    return create_async_engine(
        database_url,
        echo=echo,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=pool_pre_ping,
    )


def make_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Create an async session factory bound to the engine.

    expire_on_commit=False so attribute access after commit doesn't trigger
    a lazy refresh (which would require an async round-trip).
    """
    return async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )


async def get_session(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Async context manager yielding a session, committing on success.

    Rolls back on exception. Use inside FastAPI dependencies or ad-hoc
    scripts.
    """
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
