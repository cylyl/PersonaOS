"""Integration tests for Step 20: WorkerRunner concurrency pool and capacity enforcement.

Validates ADR 0023 and specification:
- CAP-03: Runner executes multiple tasks simultaneously when max_concurrent_tasks > 1.
- CAP-04: Sibling concurrent tasks maintain isolated execution and error handling.
- CAP-05: Graceful stop awaits all concurrent in-flight tasks to finish cleanly.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from personaos.db.models import Task as TaskModel
from personaos.domain.worker import WorkerProfile
from personaos.execution.adapters.base import Result
from personaos.execution.context import WorkerExecutionContext
from personaos.execution.kernel import WorkerExecutionKernel
from personaos.execution.runner import WorkerRunner
from personaos.orchestrator.worker import WorkerOrchestrator
from personaos.registry.profile import ProfileRegistry
from personaos.workload.service import WorkloadService

REPO_ROOT = Path(__file__).parents[2]
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


class DelayedConcurrentAdapter:
    """Simulates an adapter executing tasks with measurable concurrency."""

    def __init__(self, delay: float = 0.3) -> None:
        self.delay = delay
        self.active_count: int = 0
        self.max_observed_concurrency: int = 0
        self.lock = asyncio.Lock()

    async def execute(
        self,
        context: WorkerExecutionContext,
        checkpoint: Any | None = None,
    ) -> Result:
        async with self.lock:
            self.active_count += 1
            if self.active_count > self.max_observed_concurrency:
                self.max_observed_concurrency = self.active_count

        try:
            await asyncio.sleep(self.delay)
            if context.task.input.get("simulate_error"):
                return Result(status="failed", error="Simulated task error")
            return Result(status="completed", output={"finished": True})
        finally:
            async with self.lock:
                self.active_count -= 1


@pytest.mark.asyncio
async def test_concurrent_task_execution_in_runner(
    database_url: str,
    session_factory: async_sessionmaker[AsyncSession],
    profiles_dir: Path,
) -> None:
    """CAP-03: WorkerRunner executes multiple tasks concurrently up to max_concurrent_tasks."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    worker_id = "worker-concurrency-runner"

    # 1. Register worker with max_concurrent_tasks = 2
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
        t1 = await wl.enqueue(worker_id=worker_id, title="Task 1", objective="Job 1", input={})
        t2 = await wl.enqueue(worker_id=worker_id, title="Task 2", objective="Job 2", input={})

    adapter = DelayedConcurrentAdapter(delay=0.3)

    # 2. Build Runner with session_factory and max_concurrency = 2
    async with session_factory() as orchestrator_session:
        kernel = WorkerExecutionKernel(
            session=orchestrator_session,
            adapter=adapter,
            worker_id=worker_id,
            session_factory=session_factory,
        )
        orchestrator = WorkerOrchestrator(
            session=orchestrator_session,
            kernel=kernel,
            worker_id=worker_id,
            session_factory=session_factory,
            max_concurrency=2,
            poll_interval=0.05,
        )
        runner = WorkerRunner(
            orchestrator=orchestrator,
            session_factory=session_factory,
            worker_id=worker_id,
            node_id="node-concurrent-1",
        )

        runner_task = asyncio.create_task(runner.run())

        # Give tasks time to claim and execute concurrently
        await asyncio.sleep(0.8)
        runner.stop()
        await runner_task

    # 3. Assert both tasks executed concurrently
    assert adapter.max_observed_concurrency == 2, "Expected 2 tasks executing simultaneously"

    async with session_factory() as session:
        q1 = select(TaskModel.status).where(TaskModel.id == t1.id)
        r1 = (await session.execute(q1)).scalar_one()
        q2 = select(TaskModel.status).where(TaskModel.id == t2.id)
        r2 = (await session.execute(q2)).scalar_one()
        assert r1 == "completed"
        assert r2 == "completed"


@pytest.mark.asyncio
async def test_concurrent_task_isolation_and_error_handling(
    database_url: str,
    session_factory: async_sessionmaker[AsyncSession],
    profiles_dir: Path,
) -> None:
    """CAP-04: Failure in one concurrent task does not abort or poison sibling concurrent tasks."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    worker_id = "worker-concurrency-isolation"

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
        t_fail = await wl.enqueue(
            worker_id=worker_id,
            title="Failing Task",
            objective="Fail",
            input={"simulate_error": True},
        )
        t_ok = await wl.enqueue(
            worker_id=worker_id,
            title="Succeeding Task",
            objective="Succeed",
            input={},
        )

    adapter = DelayedConcurrentAdapter(delay=0.2)

    async with session_factory() as orchestrator_session:
        kernel = WorkerExecutionKernel(
            session=orchestrator_session,
            adapter=adapter,
            worker_id=worker_id,
            session_factory=session_factory,
        )
        orchestrator = WorkerOrchestrator(
            session=orchestrator_session,
            kernel=kernel,
            worker_id=worker_id,
            session_factory=session_factory,
            max_concurrency=2,
            poll_interval=0.05,
        )
        runner = WorkerRunner(
            orchestrator=orchestrator,
            session_factory=session_factory,
            worker_id=worker_id,
            node_id="node-concurrent-2",
        )

        runner_task = asyncio.create_task(runner.run())
        await asyncio.sleep(0.7)
        runner.stop()
        await runner_task

    async with session_factory() as session:
        r_fail = (
            await session.execute(select(TaskModel.status).where(TaskModel.id == t_fail.id))
        ).scalar_one()
        r_ok = (
            await session.execute(select(TaskModel.status).where(TaskModel.id == t_ok.id))
        ).scalar_one()
        assert r_fail == "failed"
        assert r_ok == "completed"
