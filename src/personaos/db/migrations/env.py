"""Alembic environment — async SQLAlchemy.

Pattern: standard async alembic env. Reads DATABASE_URL from
personaos.config.settings (or env var override).
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Import the v0.1 models' metadata
from personaos.db.models import Base

# Try to import settings; fall back to default if config isn't ready yet
try:
    from personaos.config import settings

    _default_url = settings.database_url
except Exception:  # pragma: no cover
    _default_url = (
        "postgresql+asyncpg://personaos:personaos@localhost:5432/personaos"
    )

config = context.config

# Override sqlalchemy.url from settings (env var / alembic.ini wins if set)
config.set_main_option(
    "sqlalchemy.url",
    config.get_main_option("sqlalchemy.url") or _default_url,
)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emits SQL without connecting)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode using an async engine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
