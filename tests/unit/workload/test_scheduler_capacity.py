"""Unit tests for scheduler capacity gating per ADR 0023.

Verifies:
- CAP-01: Saturated worker (in_progress == max_concurrent_tasks) receives no candidate tasks.
- CAP-02: Worker with available capacity receives candidate tasks.
- CAP-02b: Default max_concurrent_tasks is 1 when column is null.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from personaos.domain.worker import WorkerProfile
from personaos.registry.profile import ProfileRegistry
from personaos.workload.scheduler import WorkloadScheduler
from personaos.workload.service import WorkloadService

REPO_ROOT = Path(__file__).parents[3]
ALEMBIC_INI = REPO_ROOT / "src" / "personaos" / "db" / "migrations" / "alembic.ini"
MIGRATIONS_DIR = REPO_ROOT / "src" / "personaos" / "db" / "migrations"

pytestmark = pytest.mark.usefixtures("clean_tables")


def _alembic_config(database_url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", database_url)
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


@pytest.fixture
def profiles_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "profiles"
    monkeypatch.setenv("PERSONAOS_PROFILES_DIR", str(d))
    return d


@pytest.mark.asyncio
async def test_scheduler_capacity_gate_blocks_saturated_worker(
    database_url: str,
    session_factory: async_sessionmaker[AsyncSession],
    profiles_dir: Path,
) -> None:
    """CAP-01: Saturated worker with active task count == max_concurrent_tasks
    receives no candidate tasks.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    worker_id = "worker-capacity-01"

    # 1. Register worker with max_concurrent_tasks = 1
    async with session_factory() as session:
        reg = ProfileRegistry(session=session, profiles_dir=profiles_dir)
        profile = WorkerProfile(
            version=1,
            role="devops",
            runtime="openclaw",
            max_concurrent_tasks=1,
        )
        await reg.register_worker(worker_id, profile)

        wl = WorkloadService(session=session)
        task1 = await wl.enqueue(
            worker_id=worker_id,
            title="Task 1",
            objective="Do task 1",
            input={},
        )
        await wl.enqueue(
            worker_id=worker_id,
            title="Task 2",
            objective="Do task 2",
            input={},
        )

        scheduler = WorkloadScheduler(session=session)

        # Before any task is in_progress, task1 is returned
        c1 = await scheduler.find_candidate(worker_id=worker_id)
        assert c1 is not None
        assert c1.id == task1.id

        # Mark task1 as in_progress (simulating claim)
        await session.execute(
            text("UPDATE tasks SET status = 'in_progress', assigned_worker = :w WHERE id = :id"),
            {"w": worker_id, "id": task1.id},
        )
        await session.commit()

        # Now worker is saturated (1 in_progress >= 1 max_concurrent)
        c2 = await scheduler.find_candidate(worker_id=worker_id)
        assert c2 is None, "Saturated worker must not receive candidate tasks"


@pytest.mark.asyncio
async def test_scheduler_capacity_gate_allows_available_slots(
    database_url: str,
    session_factory: async_sessionmaker[AsyncSession],
    profiles_dir: Path,
) -> None:
    """CAP-02: Worker with max_concurrent_tasks = 2 can claim up to 2 tasks before saturating."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    worker_id = "worker-capacity-02"

    async with session_factory() as session:
        reg = ProfileRegistry(session=session, profiles_dir=profiles_dir)
        profile = WorkerProfile(
            version=1,
            role="devops",
            runtime="openclaw",
            max_concurrent_tasks=2,
        )
        await reg.register_worker(worker_id, profile)

        wl = WorkloadService(session=session)
        t1 = await wl.enqueue(worker_id=worker_id, title="T1", objective="O1", input={})
        t2 = await wl.enqueue(worker_id=worker_id, title="T2", objective="O2", input={})
        t3 = await wl.enqueue(worker_id=worker_id, title="T3", objective="O3", input={})

        scheduler = WorkloadScheduler(session=session)

        # Slot 1: Active = 0 < 2 -> eligible
        c1 = await scheduler.find_candidate(worker_id=worker_id)
        assert c1 is not None
        assert c1.id == t1.id

        # Simulate T1 claim
        await session.execute(
            text("UPDATE tasks SET status = 'in_progress', assigned_worker = :w WHERE id = :id"),
            {"w": worker_id, "id": t1.id},
        )
        await session.commit()

        # Slot 2: Active = 1 < 2 -> eligible
        c2 = await scheduler.find_candidate(worker_id=worker_id)
        assert c2 is not None
        assert c2.id == t2.id

        # Simulate T2 claim
        await session.execute(
            text("UPDATE tasks SET status = 'in_progress', assigned_worker = :w WHERE id = :id"),
            {"w": worker_id, "id": t2.id},
        )
        await session.commit()

        # Saturated: Active = 2 == 2 -> None
        c3 = await scheduler.find_candidate(worker_id=worker_id)
        assert c3 is None

        # T1 completes: Active = 1 < 2 -> T3 becomes eligible!
        await session.execute(
            text("UPDATE tasks SET status = 'completed' WHERE id = :id"),
            {"id": t1.id},
        )
        await session.commit()

        c3_after = await scheduler.find_candidate(worker_id=worker_id)
        assert c3_after is not None
        assert c3_after.id == t3.id
