"""Integration tests for Step 21: Node Lifecycle Supervision and Graceful Eviction.

Validates ADR 0024 and specification:
- SUP-INT-01: Graceful runner stop awaits in-flight task completion and marks binding.
- SUP-INT-02: Abrupt node crash triggers supervisor dead-node eviction and lease expiration.
- SUP-INT-03: Replacement compute on another node immediately reclaims and completes task.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from personaos.db.models import Task as TaskModel
from personaos.db.models import Worker as WorkerModel
from personaos.db.models import WorkerNode
from personaos.domain.worker import WorkerProfile
from personaos.execution.adapters.base import Result
from personaos.execution.context import WorkerExecutionContext
from personaos.execution.kernel import WorkerExecutionKernel
from personaos.execution.runner import WorkerRunner
from personaos.orchestrator.supervisor import NodeSupervisor
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


class DelayedExecutionAdapter:
    """Mock adapter that simulates asynchronous execution with a delay."""

    def __init__(self, delay_seconds: float = 0.3) -> None:
        self.delay_seconds = delay_seconds
        self.executed_task_ids: list[str] = []
        self.started_event = asyncio.Event()

    async def execute(
        self,
        task: Any = None,
        checkpoint: Any = None,
        context: WorkerExecutionContext | None = None,
        **kwargs: Any,
    ) -> Result:
        self.started_event.set()
        await asyncio.sleep(self.delay_seconds)
        task_id = "unknown"
        if context and context.task:
            task_id = context.task.id
        elif task is not None:
            task_id = getattr(task, "id", "unknown")
        self.executed_task_ids.append(task_id)
        return Result(status="completed", output={"msg": "finished", "task_id": task_id})


@pytest.mark.asyncio
async def test_graceful_drain_awaits_in_flight_task(
    database_url: str,
    session_factory: async_sessionmaker[AsyncSession],
    profiles_dir: Path,
) -> None:
    """SUP-INT-01: Graceful stop allows in-flight task to complete cleanly
    and records graceful_drain.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    worker_id = "w-drain-test"
    node_id = "node-drain-host"

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
        task = await wl.enqueue(
            worker_id=worker_id,
            title="Drain Task",
            objective="Wait and finish",
            input={},
        )

    adapter = DelayedExecutionAdapter(delay_seconds=0.4)

    async with session_factory() as session:
        kernel = WorkerExecutionKernel(
            session=session,
            adapter=adapter,
            worker_id=worker_id,
            node_id=node_id,
        )
        orchestrator = WorkerOrchestrator(
            session=session,
            kernel=kernel,
            poll_interval=0.05,
            worker_id=worker_id,
            node_id=node_id,
            session_factory=session_factory,
        )
        runner = WorkerRunner(
            orchestrator=orchestrator,
            session_factory=session_factory,
            worker_id=worker_id,
            node_id=node_id,
            heartbeat_interval=10.0,
            drain_timeout=5.0,
        )

        runner_task = asyncio.create_task(runner.run())

        # Wait until task execution has actually started in the adapter
        await asyncio.wait_for(adapter.started_event.wait(), timeout=5.0)
        assert runner.is_running
        # Initiate graceful stop while task is actively in-flight
        runner.stop()
        assert runner.is_draining

        # Await runner clean shutdown
        await runner_task
        assert not runner.is_running

    # Verify task completed successfully
    assert task.id in adapter.executed_task_ids

    async with session_factory() as session:
        task_status = (
            await session.execute(select(TaskModel.status).where(TaskModel.id == task.id))
        ).scalar_one()
        assert task_status == "completed"

        # Verify worker_nodes was unbound with reason = graceful_drain
        q = (
            select(WorkerNode)
            .where(WorkerNode.node_id == node_id)
            .order_by(WorkerNode.bound_at.desc())
        )
        binding = (await session.execute(q)).scalar_one()
        assert binding.unbound_at is not None
        assert binding.reason == "graceful_drain"


@pytest.mark.asyncio
async def test_node_crash_eviction_and_immediate_recovery(
    database_url: str,
    session_factory: async_sessionmaker[AsyncSession],
    profiles_dir: Path,
) -> None:
    """SUP-INT-02 & SUP-INT-03: Dead node eviction fast-expires stranded task locks,
    enabling immediate failover to a replacement compute node.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    worker_id = "w-resilient-worker"
    dead_node = "node-crash-host-1"
    survivor_node = "node-replacement-host-2"

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
        task = await wl.enqueue(
            worker_id=worker_id,
            title="Idempotent k8s deployment",
            objective="Deploy pod",
            input={"replicas": 3},
        )

        # 1. Simulate task claimed by dead_node with 15-minute lease and IDEMPOTENT classification
        now = datetime.now(UTC)
        far_future_lease = now + timedelta(minutes=15)
        await session.execute(
            text(
                """
                UPDATE tasks
                SET status = 'in_progress',
                    assigned_worker = :w,
                    assigned_node = :node,
                    side_effect_classification = 'IDEMPOTENT',
                    lock_expires_at = :lease
                WHERE id = :t_id
                """
            ),
            {"w": worker_id, "node": dead_node, "lease": far_future_lease, "t_id": task.id},
        )

        # Bind dead_node
        stale_time = now - timedelta(seconds=120)
        session.add(
            WorkerNode(
                id="binding-dead-1",
                worker_id=worker_id,
                node_id=dead_node,
                bound_at=stale_time,
            )
        )
        # Set worker heartbeat stale
        await session.execute(
            update(WorkerModel).where(WorkerModel.id == worker_id).values(last_seen_at=stale_time)
        )
        await session.commit()

        # 2. NodeSupervisor runs supervision cycle
        supervisor = NodeSupervisor(session=session)
        cycle_result = await supervisor.supervise_cycle(heartbeat_timeout_seconds=30.0)

        assert cycle_result.dead_nodes_evicted == 1
        assert cycle_result.tasks_expired == 1
        assert task.id in cycle_result.expired_task_ids

        # Verify dead node unbind
        dead_binding = await session.get(WorkerNode, "binding-dead-1")
        assert dead_binding is not None
        assert dead_binding.unbound_at is not None
        assert dead_binding.reason == "heartbeat_timeout"

        # Verify task lock is now in the past
        t_row = (
            await session.execute(
                select(TaskModel.lock_expires_at, TaskModel.status).where(TaskModel.id == task.id)
            )
        ).first()
        assert t_row is not None
        assert t_row[1] == "in_progress"
        assert t_row[0] < datetime.now(UTC)

        # 3. Replacement node immediately reclaims the task without waiting 15 minutes!
        stale_tasks = await wl.find_stale_in_progress(limit=10)
        assert any(t.id == task.id for t in stale_tasks)
        task_stale = next(t for t in stale_tasks if t.id == task.id)

        recovery_adapter = DelayedExecutionAdapter(delay_seconds=0.05)
        kernel_survivor = WorkerExecutionKernel(
            session=session,
            adapter=recovery_adapter,
            worker_id=worker_id,
            node_id=survivor_node,
        )

        # Reclaim on replacement node
        await kernel_survivor.reclaim(task_stale)
        await kernel_survivor.complete(
            task_stale,
            Result(status="completed", output={"recovered": True}),
        )

        # 4. Verify task is completed on survivor_node
        t_after = (
            await session.execute(
                select(TaskModel.status, TaskModel.assigned_node, TaskModel.result).where(
                    TaskModel.id == task.id
                )
            )
        ).first()
        assert t_after is not None
        assert t_after[0] == "completed"
        assert t_after[1] == survivor_node
        assert t_after[2] == {"recovered": True}
        assert task.id in recovery_adapter.executed_task_ids
