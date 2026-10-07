"""Alembic environment — async SQLAlchemy.

TODO(v0.1): configure Alembic to use src/personaos/db/session.py's async
engine, point to src/personaos/db/models.py's metadata, run migrations
asynchronously. Standard pattern:

    from logging.config import fileConfig
    from sqlalchemy.ext.asyncio import async_engine_from_config
    from sqlalchemy import pool
    from alembic import context
    from personaos.db.models import Base
    from personaos.config import settings

    config = context.config
    config.set_main_option("sqlalchemy.url", settings.database_url)
    target_metadata = Base.metadata
    # ... async run_migrations_online()
"""
