"""Persona Registry repository — Postgres-backed storage.

Per ADR 0002: ORM models are generated from schemas/worker.yaml and
schemas/node.yaml. Codegen deferred to scripts/codegen/ in v0.2.

TODO(v0.1): implement async SQLAlchemy queries:
  - insert/update worker rows (with version bump)
  - insert/update node rows
  - heartbeat upsert
  - list active workers, list active nodes
"""
