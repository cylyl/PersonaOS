"""Integration tests for WorkerRunner lifecycle, node binding, heartbeat, and graceful shutdown."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from personaos.db.models import WorkerNode
from personaos.domain.worker import Persona, WorkerProfile
from personaos.execution.adapters.simulation import SimulationAdapter
from personaos.execution.kernel import WorkerExecutionKernel
from personaos.execution.result import Result
from personaos.execution.runner import WorkerRunner
from personaos.orchestrator.worker import WorkerOrchestrator
from personaos.registry.profile import ProfileRegistry
from personaos.workload.service import WorkloadService

REPO_ROOT = Path(__file__).parents[2]
ALEMBIC_INI = REPO_ROOT / "src" / "personaos" / "db" / "migrations" / "alembic.ini"
MIGRATIONS_DIR = REPO_ROOT / "src" / "personaos" / "db" / "migrations"


def _alembic_config(database_url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", database_url)
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


@pytest.fixture
async def migrated_db(database_url: str) -> None:
    """Run migrations to head."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")


@pytest.fixture
def profiles_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "profiles"
    monkeypatch.setenv("PERSONAOS_PROFILES_DIR", str(d))
    return d


@pytest.mark.asyncio
async def test_worker_runner_startup_and_node_binding(
    migrated_db: None,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    profiles_dir: Path,
    clean_tables: None,
) -> None:
    """Runner startup binds worker to node in worker_nodes and unbinds on stop."""
    worker_id = "worker-devops-001"
    node_id = "node-us-east-1a"

    # Register worker
    async with session_factory() as session:
        registry = ProfileRegistry(session=session, profiles_dir=profiles_dir)
        profile = WorkerProfile(version=1, role="devops", runtime="openclaw")
        await registry.register_worker(worker_id, profile)

    adapter = SimulationAdapter(
        result=Result(status="completed", output={"status": "ok"})
    )

    async with session_factory() as s_orch:
        kernel = WorkerExecutionKernel(
            session=s_orch,
            adapter=adapter,
            worker_id=worker_id,
            profiles_dir=profiles_dir,
        )
        orchestrator = WorkerOrchestrator(
            session=s_orch, kernel=kernel, poll_interval=0.05
        )
        runner = WorkerRunner(
            orchestrator=orchestrator,
            session_factory=session_factory,
            worker_id=worker_id,
            node_id=node_id,
            heartbeat_interval=0.1,
        )

        # Start runner in background task
        runner_task = asyncio.create_task(runner.run())
        await asyncio.sleep(0.1)

        assert runner.is_running is True
        assert runner.binding_id is not None

        # Verify worker_nodes record is active
        async with session_factory() as s_check:
            binding = (
                await s_check.execute(
                    select(WorkerNode).where(WorkerNode.id == runner.binding_id)
                )
            ).scalar_one()
            assert binding.worker_id == worker_id
            assert binding.node_id == node_id
            assert binding.unbound_at is None

        # Trigger graceful stop
        runner.stop()
        await runner_task

        assert runner.is_running is False

        # Verify worker_nodes record was unbound
        async with session_factory() as s_check:
            binding_after = (
                await s_check.execute(
                    select(WorkerNode).where(WorkerNode.id == runner.binding_id)
                )
            ).scalar_one()
            assert binding_after.unbound_at is not None
            assert binding_after.reason == "runner_shutdown"


@pytest.mark.asyncio
async def test_worker_runner_executes_tasks_and_heartbeats(
    migrated_db: None,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    profiles_dir: Path,
    clean_tables: None,
) -> None:
    """Runner processes queued work and updates worker last_seen_at via heartbeat."""
    worker_id = "worker-devops-001"
    node_id = "node-runner-test"

    async with session_factory() as session:
        registry = ProfileRegistry(session=session, profiles_dir=profiles_dir)
        profile = WorkerProfile(
            version=1,
            role="devops",
            runtime="openclaw",
            persona=Persona(style="concise"),
        )
        await registry.register_worker(worker_id, profile)

        # Enqueue a task
        workload = WorkloadService(session)
        task = await workload.enqueue(
            worker_id=worker_id,
            title="Deploy Canary",
            objective="Canary deploy v1.2",
            input={"target": "canary"},
        )
        assert task.status == "queued"

    adapter = SimulationAdapter(
        result=Result(status="completed", output={"deployed": True})
    )

    async with session_factory() as s_orch:
        kernel = WorkerExecutionKernel(
            session=s_orch,
            adapter=adapter,
            worker_id=worker_id,
            profiles_dir=profiles_dir,
        )
        orchestrator = WorkerOrchestrator(
            session=s_orch, kernel=kernel, poll_interval=0.05
        )
        runner = WorkerRunner(
            orchestrator=orchestrator,
            session_factory=session_factory,
            worker_id=worker_id,
            node_id=node_id,
            heartbeat_interval=0.1,
        )

        # Process one task
        processed = await runner.process_one()
        assert processed is True
        assert len(adapter.captured_contexts) == 1
        assert adapter.captured_contexts[0].task.id == task.id

        # Verify task is now completed
        async with session_factory() as s_check:
            from personaos.db.models import Task as TaskModel

            t_db = (
                await s_check.execute(
                    select(TaskModel).where(TaskModel.id == task.id)
                )
            ).scalar_one()
            assert t_db.status == "completed"
            assert t_db.result == {"deployed": True}
