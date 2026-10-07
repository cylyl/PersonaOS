"""Async SQLAlchemy engine + session factory.

Single source of truth for database connectivity. Imported by:
  - api/deps.py (request-scoped sessions)
  - worker_main.py (long-lived session for the runner loop)
  - alembic/env.py (migrations)
  - scripts/dev/seed.py

TODO(v0.1): implement
  - create_async_engine(DATABASE_URL, pool_size=..., max_overflow=..., echo=False)
  - async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
  - get_session() async context manager
  - init_db() — verify connectivity + run pending Alembic migrations
"""
