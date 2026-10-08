"""Step 4 — Task Execution Context acceptance tests.

Per docs/specs/task-execution-context.md (ADR 0009).

  Acceptance criteria (mapped from ADR 0009):
    1. Enqueue snapshots workers.current_profile_version atomically
    2. Activation after enqueue doesn't affect the task's snapshot
    3. resolve_execution_context uses task.profile_version (NOT worker.current)
    4. Historical profiles remain resolvable after multiple activations
    5. WorkerExecutionContext is a frozen bundle of worker + profile + task
    6. End-to-end: enqueue → activate → resolve uses snapshot, NOT current
       (proves RUNTIME resolution, not just DB persistence — per Liang, 2026-10-08)
    7. No v0.2 leakage (execution_node / attempt / lease_expires_at)

These tests verify the snapshot semantics are not dead code: the
runtime MUST consume task.profile_version, never worker.current_profile_version.
"""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from personaos.db.models import Task as TaskModel
from personaos.domain.task import Task
from personaos.domain.worker import Persona, WorkerProfile
from personaos.execution.context import WorkerExecutionContext
from personaos.registry.profile import ProfileRegistry
from personaos.workload.service import WorkloadService


# tests/integration/test_task_execution_context.py → repo root → src/personaos/db/migrations
REPO_ROOT = Path(__file__).parents[2]
ALEMBIC_INI = REPO_ROOT / "src" / "personaos" / "db" / "migrations" / "alembic.ini"
MIGRATIONS_DIR = REPO_ROOT / "src" / "personaos" / "db" / "migrations"

pytestmark = pytest.mark.usefixtures("clean_tables")


def _alembic_config(database_url):
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", database_url)
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


@pytest.fixture
def profiles_dir(tmp_path, monkeypatch):
    d = tmp_path / "profiles"
    monkeypatch.setenv("PERSONAOS_PROFILES_DIR", str(d))
    return d


# ---------- AC #1: enqueue snapshots current profile version ----------


async def test_enqueue_snapshots_current_profile_version(
    profiles_dir, database_url, session_factory
):
    """WorkloadService.enqueue() captures workers.current_profile_version."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        reg = ProfileRegistry(session=session, profiles_dir=profiles_dir)
        await reg.register_worker(
            "w1",
            WorkerProfile(
                version=1,
                role="devops",
                runtime="openclaw",
                max_concurrent_tasks=1,
                persona=Persona(autonomy="medium"),
            ),
        )
        await reg.create_profile_version(
            "w1",
            WorkerProfile(
                version=2,
                role="devops",
                runtime="openclaw",
                max_concurrent_tasks=2,
                persona=Persona(autonomy="low"),
            ),
        )
        await reg.activate_profile_version("w1", 2)

        wl = WorkloadService(session=session)
        task = await wl.enqueue(
            worker_id="w1",
            title="deploy",
            objective="deploy the service",
            input={"version": "1.0"},
        )

        # The snapshot was captured at enqueue time → task.profile_version == 2
        assert task.profile_version == 2


# ---------- AC #2: activation after enqueue doesn't affect snapshot ----------


async def test_activation_after_enqueue_does_not_change_task_snapshot(
    profiles_dir, database_url, session_factory
):
    """Task A under v2 stays at v2 even after worker → v3; new Task B gets v3."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        reg = ProfileRegistry(session=session, profiles_dir=profiles_dir)
        await reg.register_worker(
            "w1",
            WorkerProfile(
                version=1,
                role="devops",
                runtime="openclaw",
                max_concurrent_tasks=1,
            ),
        )
        await reg.create_profile_version(
            "w1",
            WorkerProfile(
                version=2,
                role="devops",
                runtime="openclaw",
                max_concurrent_tasks=2,
            ),
        )
        # Activate v2 so worker.current_profile_version = 2 BEFORE the enqueue
        await reg.activate_profile_version("w1", 2)

        wl = WorkloadService(session=session)

        # Task A enqueued under v2
        task_a = await wl.enqueue(
            worker_id="w1", title="A", objective="A", input={}
        )
        assert task_a.profile_version == 2

        # Activate v3 — worker's current is now 3
        await reg.create_profile_version(
            "w1",
            WorkerProfile(
                version=3,
                role="devops",
                runtime="openclaw",
                max_concurrent_tasks=3,
            ),
        )
        await reg.activate_profile_version("w1", 3)

        # Task A still references v2 — snapshot preserved (historical contract)
        assert task_a.profile_version == 2

        # New Task B enqueued under v3 (current at enqueue time)
        task_b = await wl.enqueue(
            worker_id="w1", title="B", objective="B", input={}
        )
        assert task_b.profile_version == 3


# ---------- AC #3: resolve uses task snapshot, NOT worker current ----------


async def test_resolve_uses_task_snapshot_not_worker_current(
    profiles_dir, database_url, session_factory
):
    """resolve_execution_context(task) loads task.profile_version, not worker's."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        reg = ProfileRegistry(session=session, profiles_dir=profiles_dir)
        await reg.register_worker(
            "w1",
            WorkerProfile(
                version=1,
                role="devops",
                runtime="openclaw",
                max_concurrent_tasks=1,
                persona=Persona(autonomy="medium"),
            ),
        )
        await reg.create_profile_version(
            "w1",
            WorkerProfile(
                version=2,
                role="devops",
                runtime="openclaw",
                max_concurrent_tasks=2,
                persona=Persona(autonomy="low"),  # distinct
            ),
        )
        await reg.create_profile_version(
            "w1",
            WorkerProfile(
                version=3,
                role="devops",
                runtime="openclaw",
                max_concurrent_tasks=3,
                persona=Persona(autonomy="high"),  # distinct
            ),
        )
        await reg.activate_profile_version("w1", 3)

        # Worker is currently on v3; insert a task with profile_version=2 directly
        # (we test the resolution path independently of the enqueue path here)
        await session.execute(
            text(
                "INSERT INTO tasks (id, title, objective, input, "
                "assigned_worker, profile_version) "
                "VALUES ('t1', 'T', 'O', '{}'::jsonb, 'w1', 2)"
            )
        )
        await session.commit()

        task_row = await session.get(TaskModel, "t1")
        ctx = await reg.resolve_execution_context(Task.from_orm(task_row))

        # context.profile is v2 (the snapshot), NOT v3 (worker's current)
        assert ctx.profile.version == 2
        assert ctx.profile.persona.autonomy == "low"
        # Sanity: worker really is on v3 (so the assertion above is meaningful)
        assert ctx.worker.current_profile_version == 3


# ---------- AC #4: historical profile remains resolvable ----------


async def test_historical_profile_remains_resolvable(
    profiles_dir, database_url, session_factory
):
    """Even after v3/v4/v5 activations, the v2 task can still load v2 profile."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        reg = ProfileRegistry(session=session, profiles_dir=profiles_dir)
        await reg.register_worker(
            "w1",
            WorkerProfile(
                version=1,
                role="devops",
                runtime="openclaw",
                max_concurrent_tasks=1,
                persona=Persona(autonomy="low"),
            ),
        )
        for v, autonomy in [
            (2, "low"),
            (3, "medium"),
            (4, "medium"),
            (5, "high"),
        ]:
            await reg.create_profile_version(
                "w1",
                WorkerProfile(
                    version=v,
                    role="devops",
                    runtime="openclaw",
                    max_concurrent_tasks=v,
                    persona=Persona(autonomy=autonomy),
                ),
            )
            await reg.activate_profile_version("w1", v)

        # Sanity: worker is on v5
        worker = await reg.get_worker("w1")
        assert worker.current_profile_version == 5

        # v2 task can still resolve its v2 profile (historical, immutable)
        await session.execute(
            text(
                "INSERT INTO tasks (id, title, objective, input, "
                "assigned_worker, profile_version) "
                "VALUES ('t_v2', 'T', 'O', '{}'::jsonb, 'w1', 2)"
            )
        )
        await session.commit()

        task_row = await session.get(TaskModel, "t_v2")
        ctx = await reg.resolve_execution_context(Task.from_orm(task_row))
        assert ctx.profile.version == 2
        assert ctx.profile.persona.autonomy == "low"


# ---------- AC #5: WorkerExecutionContext bundles worker + profile + task ----------


async def test_context_bundles_worker_profile_task(
    profiles_dir, database_url, session_factory
):
    """WorkerExecutionContext is a frozen dataclass with all three fields."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        reg = ProfileRegistry(session=session, profiles_dir=profiles_dir)
        await reg.register_worker(
            "w1",
            WorkerProfile(
                version=1,
                role="devops",
                runtime="openclaw",
                max_concurrent_tasks=1,
            ),
        )

        await session.execute(
            text(
                "INSERT INTO tasks (id, title, objective, input, "
                "assigned_worker, profile_version) "
                "VALUES ('t1', 'T', 'O', '{}'::jsonb, 'w1', 1)"
            )
        )
        await session.commit()

        task_row = await session.get(TaskModel, "t1")
        ctx = await reg.resolve_execution_context(Task.from_orm(task_row))

        # All three fields are present and have the right values
        assert isinstance(ctx, WorkerExecutionContext)
        assert ctx.worker.id == "w1"
        assert ctx.profile.version == 1
        assert ctx.task.id == "t1"

        # Frozen — cannot reassign fields (snapshot contract in the type)
        with pytest.raises(FrozenInstanceError):
            ctx.profile = None  # type: ignore[misc]


# ---------- AC #6: end-to-end — enqueue → activate → resolve uses snapshot ----------


async def test_enqueue_then_activate_then_resolve_uses_snapshot(
    profiles_dir, database_url, session_factory
):
    """End-to-end: enqueue Task A under v2, activate v3, resolve → context.profile.version == 2.

    Per Liang (2026-10-08): this is the runtime-resolution test, not just DB
    persistence. Proves the snapshot contract holds through the full lifecycle:

        v2 active
         ↓
        enqueue Task A → Task A.profile_version = 2 (snapshotted)
         ↓
        activate v3 → workers.current_profile_version = 3
         ↓
        resolve_execution_context(Task A)
         ↓
        context.profile.version == 2 (NOT 3)
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        reg = ProfileRegistry(session=session, profiles_dir=profiles_dir)
        await reg.register_worker(
            "w1",
            WorkerProfile(
                version=1,
                role="devops",
                runtime="openclaw",
                max_concurrent_tasks=1,
                persona=Persona(autonomy="medium"),
            ),
        )
        await reg.create_profile_version(
            "w1",
            WorkerProfile(
                version=2,
                role="devops",
                runtime="openclaw",
                max_concurrent_tasks=2,
                persona=Persona(autonomy="low"),  # distinct from v1/v3
            ),
        )
        await reg.activate_profile_version("w1", 2)

        # Enqueue Task A — snapshot captures v2
        wl = WorkloadService(session=session)
        task_a = await wl.enqueue(
            worker_id="w1", title="A", objective="A", input={}
        )
        assert task_a.profile_version == 2

        # Activate v3 — worker's current is now 3 (different persona)
        await reg.create_profile_version(
            "w1",
            WorkerProfile(
                version=3,
                role="devops",
                runtime="openclaw",
                max_concurrent_tasks=3,
                persona=Persona(autonomy="high"),  # different from v2's "low"
            ),
        )
        await reg.activate_profile_version("w1", 3)

        # Resolve Task A — context.profile MUST be v2 (snapshot), NOT v3
        ctx = await reg.resolve_execution_context(task_a)
        assert ctx.profile.version == 2
        # v2's autonomy was "low" — if we'd accidentally read v3, this would be "high"
        assert ctx.profile.persona.autonomy == "low"
        # Sanity: worker IS on v3 (so the assertion above is meaningful)
        assert ctx.worker.current_profile_version == 3
        # Context is frozen
        assert isinstance(ctx, WorkerExecutionContext)


# ---------- AC #7: no v0.2 leakage (static check on migration 0002) ----------


def test_no_v0_2_columns_in_migration_0002():
    """Migration 0002 didn't add execution_node / attempt / lease_expires_at.

    Parses the migration with ast and checks only the upgrade() function
    body, looking for quoted column names. The docstring (which mentions
    these as "Deferred to v0.2+") is intentionally excluded — words in
    a deferred-list comment are not the same as a leaked column.
    """
    import ast

    p = (
        Path(__file__).parents[2]
        / "src"
        / "personaos"
        / "db"
        / "migrations"
        / "versions"
        / "0002_profile_version.py"
    )
    src = p.read_text(encoding="utf-8")
    tree = ast.parse(src)

    upgrade_body = ""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "upgrade":
            upgrade_body = ast.get_source_segment(src, node) or ""
            break

    assert upgrade_body, "migration 0002 has no upgrade() function"

    for forbidden in ("execution_node", "attempt", "lease_expires_at"):
        if f'"{forbidden}"' in upgrade_body or f"'{forbidden}'" in upgrade_body:
            raise AssertionError(
                f"v0.2 field {forbidden!r} leaked into migration 0002 upgrade()"
            )
