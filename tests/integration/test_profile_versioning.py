"""Step 3 — WorkerProfile versioning integration tests.

Per docs/specs/worker-profile.md:

  Invariants tested:
    1. One active profile version per worker
    2. Profile version immutable after activation (file write-once)
    3. Every task captures exact profile version at enqueue
    4. Historical tasks remain reproducible
    5. Activation is atomic (single DB transaction)
    6. Registry owns profile lifecycle

CRITICAL: tasks.profile_version records the configuration ASSIGNED to the
task, NOT necessarily the configuration that ultimately executed it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from personaos.db.models import Worker as WorkerModel
from personaos.domain.worker import Persona, WorkerProfile, Worker
from personaos.registry.profile import (
    ProfileRegistry,
    ProfileVersionExistsError,
    ProfileVersionNotFoundError,
    WorkerNotFoundError,
    ProfileError,
)


# tests/integration/test_profile_versioning.py → repo root → src/personaos/db/migrations
REPO_ROOT = Path(__file__).parents[2]
ALEMBIC_INI = REPO_ROOT / "src" / "personaos" / "db" / "migrations" / "alembic.ini"
MIGRATIONS_DIR = REPO_ROOT / "src" / "personaos" / "db" / "migrations"

# Every test in this module starts with a clean DB.
# The fixture skips the truncate if the tables don't exist yet
# (e.g. before test_migration_0002_adds_profile_version_columns has run).
pytestmark = pytest.mark.usefixtures("clean_tables")


def _alembic_config(database_url):
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", database_url)
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


# Use a temp directory for profiles; registry reads PERSONAOS_PROFILES_DIR via env
@pytest.fixture
def profiles_dir(tmp_path, monkeypatch):
    d = tmp_path / "profiles"
    monkeypatch.setenv("PERSONAOS_PROFILES_DIR", str(d))
    return d


# ---------- Migration 0002 ----------


async def test_migration_0002_adds_profile_version_columns(database_url):
    """Migration 0002 adds current_profile_version + profile_version columns."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:

            def check(sync_conn):
                insp = inspect(sync_conn)
                workers_cols = {c["name"] for c in insp.get_columns("workers")}
                tasks_cols = {c["name"] for c in insp.get_columns("tasks")}
                assert "current_profile_version" in workers_cols
                assert "profile_version" in tasks_cols

            await conn.run_sync(check)
    finally:
        await engine.dispose()


async def test_default_value_for_profile_version_columns(database_url):
    """Both new columns default to 1."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

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
                        "SELECT current_profile_version FROM workers WHERE id = 'w1'"
                    )
                )
            ).first()
            assert row[0] == 1

            await conn.execute(
                text(
                    "INSERT INTO tasks (id, title, objective, input) "
                    "VALUES ('t1', 'T', 'O', '{}'::jsonb)"
                )
            )
            row = (
                await conn.execute(
                    text("SELECT profile_version FROM tasks WHERE id = 't1'")
                )
            ).first()
            assert row[0] == 1
    finally:
        await engine.dispose()


async def test_migration_0002_downgrade_drops_columns(database_url):
    """Downgrade to 0001 removes the new columns."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")
    await asyncio.to_thread(command.downgrade, cfg, "0001")

    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:

            def check(sync_conn):
                insp = inspect(sync_conn)
                workers_cols = {c["name"] for c in insp.get_columns("workers")}
                tasks_cols = {c["name"] for c in insp.get_columns("tasks")}
                assert "current_profile_version" not in workers_cols
                assert "profile_version" not in tasks_cols

            await conn.run_sync(check)
    finally:
        await engine.dispose()


# ---------- Profile directory operations (sync, no DB) ----------


def test_profile_path_construction():
    """Profile paths follow profiles/{worker_id}/v{version}.yaml."""
    reg = ProfileRegistry(session=None, profiles_dir=Path("/tmp/test"))
    assert reg._profile_path("worker-devops-001", 2) == Path(
        "/tmp/test/worker-devops-001/v2.yaml"
    )


def test_list_profile_versions_empty_when_no_dir(profiles_dir):
    """Empty/missing worker dir returns empty list."""
    reg = ProfileRegistry(session=None, profiles_dir=profiles_dir)
    assert reg.list_profile_versions("worker-1") == []


def test_list_profile_versions_sorted(profiles_dir):
    """Versions returned in sorted order."""
    worker_dir = profiles_dir / "worker-1"
    worker_dir.mkdir(parents=True)
    for v in [3, 1, 2]:
        (worker_dir / f"v{v}.yaml").write_text(
            "version: 1\nrole: devops\nruntime: openclaw\n"
            "max_concurrent_tasks: 1\npersona:\n  style: cautious\n",
            encoding="utf-8",
        )
    reg = ProfileRegistry(session=None, profiles_dir=profiles_dir)
    assert reg.list_profile_versions("worker-1") == [1, 2, 3]


def test_get_profile_version_roundtrip(profiles_dir):
    """Write a profile, read it back, get equivalent dataclass."""
    reg = ProfileRegistry(session=None, profiles_dir=profiles_dir)
    profile = WorkerProfile(
        version=2,
        role="devops",
        description="Infra engineer",
        persona=Persona(
            style="cautious",
            communication="concise",
            autonomy="medium",
            behavior={"verify_first": True},
            goals=["reliability"],
            constraints=["never delete prod"],
        ),
        skills=["kubernetes"],
        permissions=["staging.read"],
        runtime="openclaw",
        max_concurrent_tasks=2,
    )
    reg._write_profile_file("worker-1", profile)

    loaded = reg.get_profile_version("worker-1", 2)
    assert loaded == profile


def test_get_profile_version_not_found(profiles_dir):
    """Non-existent version raises ProfileVersionNotFoundError."""
    reg = ProfileRegistry(session=None, profiles_dir=profiles_dir)
    with pytest.raises(ProfileVersionNotFoundError):
        reg.get_profile_version("worker-1", 999)


# ---------- Immutability guard (Invariant #2) ----------


def test_immutability_create_rejects_overwrite(profiles_dir):
    """Creating v1 twice raises ProfileVersionExistsError."""
    reg = ProfileRegistry(session=None, profiles_dir=profiles_dir)
    profile = WorkerProfile(
        version=1,
        role="devops",
        runtime="openclaw",
        max_concurrent_tasks=1,
        persona=Persona(style="cautious"),
    )
    reg._write_profile_file("worker-1", profile)
    with pytest.raises(ProfileVersionExistsError):
        reg._write_profile_file("worker-1", profile)


# ---------- Activation atomicity (Invariants #1, #2, #5) ----------


async def test_activation_updates_db_not_file(
    profiles_dir, database_url, session_factory
):
    """Activation updates workers.current_profile_version; profile file unchanged."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        reg = ProfileRegistry(session=session, profiles_dir=profiles_dir)
        v1 = WorkerProfile(
            version=1,
            role="devops",
            runtime="openclaw",
            max_concurrent_tasks=1,
            persona=Persona(style="cautious", autonomy="medium"),
            skills=["kubernetes"],
        )
        await reg.register_worker("worker-1", v1)

        # Capture v1 file content + mtime
        v1_file = profiles_dir / "worker-1" / "v1.yaml"
        v1_content_before = v1_file.read_text(encoding="utf-8")
        v1_mtime_before = v1_file.stat().st_mtime_ns

        # Create + activate v2 (different persona)
        v2 = WorkerProfile(
            version=2,
            role="devops",
            runtime="openclaw",
            max_concurrent_tasks=2,  # changed
            persona=Persona(style="cautious", autonomy="low"),  # changed
            skills=["kubernetes", "ansible"],  # changed
        )
        await reg.create_profile_version("worker-1", v2)
        await reg.activate_profile_version("worker-1", 2)

        # 1. workers.current_profile_version is now 2
        worker = await reg.get_worker("worker-1")
        assert worker.current_profile_version == 2
        assert worker.max_concurrent_tasks == 2
        assert "ansible" in worker.capabilities
        assert worker.personality.get("autonomy") == "low"

        # 2. v1 file UNCHANGED (immutability invariant)
        v1_content_after = v1_file.read_text(encoding="utf-8")
        v1_mtime_after = v1_file.stat().st_mtime_ns
        assert v1_content_before == v1_content_after
        assert v1_mtime_before == v1_mtime_after


async def test_activation_rollback_on_invalid_version(
    profiles_dir, database_url, session_factory
):
    """Activating a non-existent version raises; workers.current_profile_version unchanged."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        reg = ProfileRegistry(session=session, profiles_dir=profiles_dir)
        v1 = WorkerProfile(
            version=1, role="devops", runtime="openclaw", max_concurrent_tasks=1
        )
        await reg.register_worker("worker-1", v1)

        with pytest.raises(ProfileVersionNotFoundError):
            await reg.activate_profile_version("worker-1", 999)

        # workers.current_profile_version should still be 1 (from registration)
        worker = await reg.get_worker("worker-1")
        assert worker.current_profile_version == 1


async def test_get_worker_unknown_raises(
    profiles_dir, database_url, session_factory
):
    """get_worker on an unknown worker_id raises WorkerNotFoundError."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        reg = ProfileRegistry(session=session, profiles_dir=profiles_dir)
        with pytest.raises(WorkerNotFoundError):
            await reg.get_worker("nonexistent-worker")


# ---------- Task snapshot (Invariants #3, #4) ----------


async def test_task_profile_version_snapshot_is_preserved(
    profiles_dir, database_url, session_factory
):
    """tasks.profile_version captured at enqueue is preserved across worker activations.

    CRITICAL: tasks.profile_version records the configuration ASSIGNED to the
    task, not necessarily the configuration that ultimately executed it.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        # Worker starts at v2
        await session.execute(
            text(
                "INSERT INTO workers (id, name, role, current_profile_version) "
                "VALUES ('w1', 'W1', 'devops', 2)"
            )
        )
        # Task enqueued under v2
        await session.execute(
            text(
                "INSERT INTO tasks (id, title, objective, input, "
                "assigned_worker, profile_version) "
                "VALUES ('t1', 'T', 'O', '{}'::jsonb, 'w1', 2)"
            )
        )
        await session.commit()

        # Worker activated to v3 (later)
        await session.execute(
            text("UPDATE workers SET current_profile_version = 3 WHERE id = 'w1'")
        )
        await session.commit()

        # Task STILL references v2 — snapshot is preserved (historical contract)
        row = (
            await session.execute(
                text("SELECT profile_version FROM tasks WHERE id = 't1'")
            )
        ).first()
        assert row[0] == 2


# ---------- Full lifecycle (end-to-end) ----------


async def test_full_lifecycle_create_v2_then_activate(
    profiles_dir, database_url, session_factory
):
    """End-to-end: register v1, create v2 with different persona, activate, verify."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        reg = ProfileRegistry(session=session, profiles_dir=profiles_dir)

        # Register v1
        v1 = WorkerProfile(
            version=1,
            role="devops",
            runtime="openclaw",
            max_concurrent_tasks=1,
            persona=Persona(style="cautious", autonomy="medium"),
            skills=["kubernetes"],
        )
        await reg.register_worker("worker-devops-001", v1)

        # Both the file and the DB row exist
        assert reg.list_profile_versions("worker-devops-001") == [1]
        worker = await reg.get_worker("worker-devops-001")
        assert worker.current_profile_version == 1

        # Create v2 (different persona)
        v2 = WorkerProfile(
            version=2,
            role="devops",
            runtime="openclaw",
            max_concurrent_tasks=2,
            persona=Persona(style="cautious", autonomy="low"),
            skills=["kubernetes", "terraform"],
        )
        await reg.create_profile_version("worker-devops-001", v2)

        # Both v1 and v2 files exist; worker still on v1
        assert reg.list_profile_versions("worker-devops-001") == [1, 2]
        worker = await reg.get_worker("worker-devops-001")
        assert worker.current_profile_version == 1

        # Activate v2
        await reg.activate_profile_version("worker-devops-001", 2)

        # Worker is now on v2; v1 file untouched
        worker = await reg.get_worker("worker-devops-001")
        assert worker.current_profile_version == 2
        assert worker.max_concurrent_tasks == 2
        assert "terraform" in worker.capabilities

        v1_content = (profiles_dir / "worker-devops-001" / "v1.yaml").read_text(
            encoding="utf-8"
        )
        v2_content = (profiles_dir / "worker-devops-001" / "v2.yaml").read_text(
            encoding="utf-8"
        )
        # v1 has autonomy medium (immutable), v2 has autonomy low (active)
        assert "autonomy: medium" in v1_content
        assert "autonomy: low" in v2_content

        # get_current_profile returns v2
        current = await reg.get_current_profile("worker-devops-001")
        assert current.version == 2
        assert current.persona.autonomy == "low"

        # get_profile_version returns v1 (immutable historical)
        historical = reg.get_profile_version("worker-devops-001", 1)
        assert historical.version == 1
        assert historical.persona.autonomy == "medium"
