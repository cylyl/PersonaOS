"""Database layer — async SQLAlchemy engine, session factory, ORM models.

Per ADR 0002, ORM models are generated from schemas/*.yaml (codegen deferred
to scripts/codegen/ in v0.2). Migrations are Alembic.
"""
