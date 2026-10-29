"""Step 1 acceptance tests: PostgreSQL schema migration.

Verifies (per Liang, 2026-10-07):
  1. Migration applies cleanly from a fresh database.
  2. All six tables are created with no v0.2 leaks.
  3. CHECK constraints enforced on status / priority / event type.
  4. JSONB defaults work.
  5. Foreign keys work (CASCADE, SET NULL).
  6. Circular FK between tasks ↔ task_checkpoints works.
  7. Indexes are present.
  8. Downgrade drops all tables cleanly.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine


# tests/integration/test_migration.py → repo root → src/personaos/db/migrations
REPO_ROOT = Path(__file__).parents[2]
ALEMBIC_INI = REPO_ROOT / "src" / "personaos" / "db" / "migrations" / "alembic.ini"
MIGRATIONS_DIR = REPO_ROOT / "src" / "personaos" / "db" / "migrations"

# Every test in this module starts with a clean DB.
# The fixture skips the truncate if the tables don't exist yet
# (e.g. before test_migration_upgrade_runs_cleanly has run).
pytestmark = pytest.mark.usefixtures("clean_tables")


def _alembic_config(database_url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", database_url)
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


# ---------- 1. Migration applies to a clean database ---------- #


def test_migration_upgrade_runs_cleanly(database_url):
    """Migration runs to 'head' on an empty database without errors."""
    cfg = _alembic_config(database_url)
    command.upgrade(cfg, "head")


# ---------- 2. All six tables exist, no v0.2 leaks ---------- #


async def test_all_six_tables_exist(database_url):
    """Verify the six v0.1 tables are created."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:

            def check(sync_conn):
                insp = inspect(sync_conn)
                tables = set(insp.get_table_names())
                expected = {
                    "workers",
                    "tasks",
                    "worker_skills",
                    "worker_nodes",
                    "task_events",
                    "task_checkpoints",
                }
                missing = expected - tables
                assert not missing, f"missing tables: {missing}"
                forbidden = {
                    "policies",
                    "leases",
                    "pending_approvals",
                    "events",
                    "nodes",
                    "memories",
                    "skills",
                    "approvals",
                }
                leaked = tables & forbidden
                assert not leaked, f"v0.2 tables leaked into v0.1: {leaked}"

            await conn.run_sync(check)
    finally:
        await engine.dispose()

# ---------- 3. CHECK constraints enforced ---------- #


async def test_workers_status_check_constraint(database_url):
    """workers.status CHECK constraint rejects unknown values."""
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO workers (id, name, role, status) "
                    "VALUES ('w1', 'W1', 'devops', 'idle')"
                )
            )
            with pytest.raises(Exception):
                await conn.execute(
                    text(
                        "INSERT INTO workers (id, name, role, status) "
                        "VALUES ('w2', 'W2', 'devops', 'unknown_status')"
                    )
                )
    finally:
        await engine.dispose()

async def test_tasks_status_and_priority_check_constraints(database_url):
    """tasks.status and tasks.priority CHECK constraints reject unknown values."""
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO workers (id, name, role) "
                    "VALUES ('w1', 'W1', 'devops')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO tasks (id, title, objective, status, priority, input) "
                    "VALUES ('t1', 'T', 'O', 'queued', 'high', '{}'::jsonb)"
                )
            )
            with pytest.raises(Exception):
                await conn.execute(
                    text(
                        "INSERT INTO tasks (id, title, objective, status, priority, input) "
                        "VALUES ('t2', 'T', 'O', 'frobnicated', 'high', '{}'::jsonb)"
                    )
                )
            with pytest.raises(Exception):
                await conn.execute(
                    text(
                        "INSERT INTO tasks (id, title, objective, status, priority, input) "
                        "VALUES ('t3', 'T', 'O', 'queued', 'urgent', '{}'::jsonb)"
                    )
                )
    finally:
        await engine.dispose()

async def test_task_events_type_check_constraint(database_url):
    """task_events.type CHECK constraint rejects v0.2 event types."""
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO workers (id, name, role) "
                    "VALUES ('w1', 'W1', 'devops')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO tasks (id, title, objective, input) "
                    "VALUES ('t1', 'T', 'O', '{}'::jsonb)"
                )
            )
            # Valid v0.1 type
            await conn.execute(
                text(
                    "INSERT INTO task_events (task_id, type, actor_id) "
                    "VALUES ('t1', 'task.created', 'system')"
                )
            )
            # Valid Step 15 / ADR 0018 type
            await conn.execute(
                text(
                    "INSERT INTO task_events (task_id, type, actor_id) "
                    "VALUES ('t1', 'task.requeued', 'system')"
                )
            )
            # Valid Step 17 / ADR 0020 types
            await conn.execute(
                text(
                    "INSERT INTO task_events (task_id, type, actor_id) "
                    "VALUES ('t1', 'policy.allowed', 'system')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO task_events (task_id, type, actor_id) "
                    "VALUES ('t1', 'policy.denied', 'system')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO task_events (task_id, type, actor_id) "
                    "VALUES ('t1', 'approval.requested', 'system')"
                )
            )
            # v0.2 type (worker.created) should be rejected
            with pytest.raises(Exception):
                await conn.execute(
                    text(
                        "INSERT INTO task_events (task_id, type, actor_id) "
                        "VALUES ('t1', 'worker.created', 'system')"
                    )
                )
    finally:
        await engine.dispose()

# ---------- 4. JSONB defaults work ---------- #


async def test_workers_jsonb_defaults(database_url):
    """workers.capabilities/permissions/personality default correctly."""
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO workers (id, name, role) "
                    "VALUES ('w1', 'W1', 'devops')"
                )
            )
            row = (
                await conn.execute(
                    text(
                        "SELECT capabilities, permissions, personality "
                        "FROM workers WHERE id = 'w1'"
                    )
                )
            ).first()
            assert row[0] == []
            assert row[1] == []
            assert row[2] == {}
    finally:
        await engine.dispose()

# ---------- 5. Foreign keys work ---------- #


async def test_tasks_assigned_worker_fk_set_null(database_url):
    """Deleting a worker sets tasks.assigned_worker to NULL (no orphans)."""
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO workers (id, name, role) "
                    "VALUES ('w1', 'W1', 'devops')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO tasks (id, title, objective, assigned_worker, input) "
                    "VALUES ('t1', 'T', 'O', 'w1', '{}'::jsonb)"
                )
            )
            row = (
                await conn.execute(
                    text("SELECT assigned_worker FROM tasks WHERE id = 't1'")
                )
            ).first()
            assert row[0] == "w1"
            # Delete worker; task.assigned_worker should become NULL
            await conn.execute(text("DELETE FROM workers WHERE id = 'w1'"))
            row = (
                await conn.execute(
                    text("SELECT assigned_worker FROM tasks WHERE id = 't1'")
                )
            ).first()
            assert row[0] is None
            # Task itself is preserved (SET NULL, not CASCADE)
            row = (
                await conn.execute(text("SELECT id FROM tasks WHERE id = 't1'"))
            ).first()
            assert row is not None
    finally:
        await engine.dispose()

async def test_worker_skills_fk_cascade(database_url):
    """Deleting a worker CASCADEs worker_skills rows."""
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO workers (id, name, role) "
                    "VALUES ('w1', 'W1', 'devops')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO worker_skills (worker_id, skill) "
                    "VALUES ('w1', 'kubernetes')"
                )
            )
            count = (
                await conn.execute(
                    text("SELECT COUNT(*) FROM worker_skills WHERE worker_id = 'w1'")
                )
            ).scalar()
            assert count == 1
            await conn.execute(text("DELETE FROM workers WHERE id = 'w1'"))
            count = (
                await conn.execute(
                    text("SELECT COUNT(*) FROM worker_skills WHERE worker_id = 'w1'")
                )
            ).scalar()
            assert count == 0
    finally:
        await engine.dispose()

# ---------- 6. Circular FK between tasks ↔ task_checkpoints ---------- #


async def test_tasks_to_task_checkpoints_fk_set_null(database_url):
    """The circular FK between tasks.checkpoint_ref and task_checkpoints.ref works."""
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO workers (id, name, role) "
                    "VALUES ('w1', 'W1', 'devops')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO tasks (id, title, objective, input) "
                    "VALUES ('t1', 'T', 'O', '{}'::jsonb)"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO task_checkpoints (ref, task_id, state) "
                    "VALUES ('cp-1', 't1', '{\"step\": 1}'::jsonb)"
                )
            )
            await conn.execute(
                text("UPDATE tasks SET checkpoint_ref = 'cp-1' WHERE id = 't1'")
            )
            row = (
                await conn.execute(
                    text("SELECT checkpoint_ref FROM tasks WHERE id = 't1'")
                )
            ).first()
            assert row[0] == "cp-1"
            await conn.execute(
                text("DELETE FROM task_checkpoints WHERE ref = 'cp-1'")
            )
            row = (
                await conn.execute(
                    text("SELECT checkpoint_ref FROM tasks WHERE id = 't1'")
                )
            ).first()
            assert row[0] is None
    finally:
        await engine.dispose()

async def test_task_checkpoints_to_tasks_fk_cascade(database_url):
    """Deleting a task CASCADEs its checkpoints."""
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO workers (id, name, role) "
                    "VALUES ('w1', 'W1', 'devops')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO tasks (id, title, objective, input) "
                    "VALUES ('t1', 'T', 'O', '{}'::jsonb)"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO task_checkpoints (ref, task_id, state) "
                    "VALUES ('cp-1', 't1', '{}'::jsonb)"
                )
            )
            await conn.execute(text("DELETE FROM tasks WHERE id = 't1'"))
            count = (
                await conn.execute(
                    text(
                        "SELECT COUNT(*) FROM task_checkpoints WHERE task_id = 't1'"
                    )
                )
            ).scalar()
            assert count == 0
    finally:
        await engine.dispose()

# ---------- 7. Indexes are present ---------- #


async def test_all_indexes_present(database_url):
    """Verify the expected indexes are created."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:

            def check(sync_conn):
                insp = inspect(sync_conn)
                expected = {
                    "workers": {"ix_workers_status", "ix_workers_role"},
                    "worker_skills": {"ix_worker_skills_skill"},
                    "worker_nodes": {
                        "ix_worker_nodes_worker",
                        "ix_worker_nodes_node",
                        "ix_worker_nodes_current",
                    },
                    "tasks": {
                        "ix_tasks_status",
                        "ix_tasks_assigned_worker",
                        "ix_tasks_assigned_node",
                        "ix_tasks_priority_created",
                        "ix_tasks_simulated_from",
                        "ix_tasks_recovery_scan",
                        "ix_tasks_review_queue",
                    },
                    "task_checkpoints": {"ix_task_checkpoints_task"},
                    "task_events": {"ix_task_events_task_created"},
                }
                for table, ix_names in expected.items():
                    actual = {ix["name"] for ix in insp.get_indexes(table)}
                    missing = ix_names - actual
                    assert not missing, f"{table}: missing indexes {missing}"

            await conn.run_sync(check)
    finally:
        await engine.dispose()

# ---------- 8. Downgrade drops everything cleanly ---------- #


async def test_downgrade_drops_all_tables(database_url):
    """Downgrade to base removes all six tables."""
    cfg = _alembic_config(database_url)
    # Run sync alembic commands in a thread to avoid asyncio.run() conflict
    # with the running event loop in this async test.
    await asyncio.to_thread(command.upgrade, cfg, "head")
    await asyncio.to_thread(command.downgrade, cfg, "base")

    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:

            def check(sync_conn):
                insp = inspect(sync_conn)
                tables = set(insp.get_table_names())
                v01_tables = {
                    "workers",
                    "tasks",
                    "worker_skills",
                    "worker_nodes",
                    "task_events",
                    "task_checkpoints",
                }
                remaining = v01_tables & tables
                assert not remaining, f"tables still present after downgrade: {remaining}"

            await conn.run_sync(check)
    finally:
        await engine.dispose()

def test_upgrade_after_downgrade_is_idempotent(database_url):
    """Up → down → up works (no stale state)."""
    cfg = _alembic_config(database_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")  # Must succeed


async def test_migration_0005_upgrade_downgrade_data_preservation(database_url):
    """Migration 0005: task.requeued is valid; downgrade preserves data without errors."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            # Seed worker and task
            await conn.execute(
                text("INSERT INTO workers (id, name, role) VALUES ('w1', 'W1', 'devops')")
            )
            await conn.execute(
                text(
                    "INSERT INTO tasks (id, title, objective, assigned_worker, input) "
                    "VALUES ('t1', 'T', 'O', 'w1', '{}'::jsonb)"
                )
            )
            # Insert task.requeued event
            await conn.execute(
                text(
                    "INSERT INTO task_events (task_id, type, actor_id, payload) "
                    "VALUES ('t1', 'task.requeued', 'op-alice', "
                    "'{\"reason\": \"re-queued\"}'::jsonb)"
                )
            )

        # Downgrade to 0004
        await asyncio.to_thread(command.downgrade, cfg, "0004")

        # Verify historical data was preserved as task.created with original_type marker
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text("SELECT type, payload FROM task_events WHERE task_id = 't1'")
                )
            ).first()
            assert row is not None
            assert row[0] == "task.created"
            assert row[1]["original_type"] == "task.requeued"
            assert row[1]["reason"] == "re-queued"

        # Upgrade back to head (0006)
        await asyncio.to_thread(command.upgrade, cfg, "head")

        # Verify historical data was fully restored back to task.requeued with original payload
        async with engine.begin() as conn:
            row_restored = (
                await conn.execute(
                    text("SELECT type, payload FROM task_events WHERE task_id = 't1'")
                )
            ).first()
            assert row_restored is not None
            assert row_restored[0] == "task.requeued"
            assert "original_type" not in row_restored[1]
            assert row_restored[1]["reason"] == "re-queued"
    finally:
        await engine.dispose()

async def test_migration_0006_upgrade_downgrade_data_preservation(database_url):
    """Migration 0006: security events roundtrip cleanly across downgrade to 0005 and upgrade."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO workers (id, name, role) VALUES ('w2', 'W2', 'devops')")
            )
            await conn.execute(
                text(
                    "INSERT INTO tasks (id, title, objective, assigned_worker, input) "
                    "VALUES ('t2', 'T2', 'O2', 'w2', '{}'::jsonb)"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO task_events (task_id, type, actor_id, payload) "
                    "VALUES ('t2', 'policy.denied', 'security:policy', "
                    "'{\"rule_id\": \"r1\"}'::jsonb)"
                )
            )

        # Downgrade to 0005
        await asyncio.to_thread(command.downgrade, cfg, "0005")

        # Verify preserved as task.failed with marker
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text("SELECT type, payload FROM task_events WHERE task_id = 't2'")
                )
            ).first()
            assert row is not None
            assert row[0] == "task.failed"
            assert row[1]["original_type"] == "policy.denied"
            assert row[1]["rule_id"] == "r1"

        # Upgrade back to head
        await asyncio.to_thread(command.upgrade, cfg, "head")

        # Verify fully restored back to policy.denied
        async with engine.begin() as conn:
            row_restored = (
                await conn.execute(
                    text("SELECT type, payload FROM task_events WHERE task_id = 't2'")
                )
            ).first()
            assert row_restored is not None
            assert row_restored[0] == "policy.denied"
            assert "original_type" not in row_restored[1]
            assert row_restored[1]["rule_id"] == "r1"
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_migration_0007_upgrade_downgrade(database_url):
    """Test 0007 migration: control_plane_nodes table and tasks.claimed_by_cp_id roundtrip."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            # 1. Verify control_plane_nodes can be inserted into
            await conn.execute(
                text(
                    "INSERT INTO control_plane_nodes "
                    "(id, hostname, ip_address, port, api_endpoint, version, status, metadata) "
                    "VALUES ('cp-node-1', 'host1.internal', '10.0.0.1', 8000, "
                    "'https://host1.internal:8000', '0.2.0', 'active', '{\"rack\": \"r1\"}'::jsonb)"
                )
            )
            # 2. Verify tasks.claimed_by_cp_id column
            await conn.execute(
                text("INSERT INTO workers (id, name, role) VALUES ('w3', 'W3', 'devops')")
            )
            await conn.execute(
                text(
                    "INSERT INTO tasks (id, title, objective, assigned_worker, claimed_by_cp_id, input) "
                    "VALUES ('t3', 'T3', 'O3', 'w3', 'cp-node-1', '{}'::jsonb)"
                )
            )

        # Query back
        async with engine.begin() as conn:
            cp_row = (
                await conn.execute(
                    text("SELECT hostname, status, metadata FROM control_plane_nodes WHERE id = 'cp-node-1'")
                )
            ).first()
            assert cp_row is not None
            assert cp_row[0] == "host1.internal"
            assert cp_row[1] == "active"
            assert cp_row[2]["rack"] == "r1"

            task_row = (
                await conn.execute(
                    text("SELECT claimed_by_cp_id FROM tasks WHERE id = 't3'")
                )
            ).first()
            assert task_row is not None
            assert task_row[0] == "cp-node-1"

        # Downgrade to 0006
        await asyncio.to_thread(command.downgrade, cfg, "0006")

        async with engine.connect() as conn:
            def check_downgraded(sync_conn):
                insp = inspect(sync_conn)
                tables = set(insp.get_table_names())
                assert "control_plane_nodes" not in tables
                task_cols = {c["name"] for c in insp.get_columns("tasks")}
                assert "claimed_by_cp_id" not in task_cols

            await conn.run_sync(check_downgraded)

        # Upgrade back to head
        await asyncio.to_thread(command.upgrade, cfg, "head")

        async with engine.connect() as conn:
            def check_upgraded(sync_conn):
                insp = inspect(sync_conn)
                tables = set(insp.get_table_names())
                assert "control_plane_nodes" in tables
                task_cols = {c["name"] for c in insp.get_columns("tasks")}
                assert "claimed_by_cp_id" in task_cols

            await conn.run_sync(check_upgraded)
    finally:
        await engine.dispose()
