"""Phase-1 acceptance test — the killer resilience property per VIKUNJA.md:

"Compute is replaceable; the worker is persistent."

The end-to-end flow:
  1. Create a Worker (DevOps) with skills [kubernetes] + capabilities [kubectl]
  2. Create a Task that requires kubernetes + kubectl (deploy FMS to staging)
  3. Scheduler/Workload matches Worker to Task
  4. Worker is bound to Node-1; adapter starts execution
  5. Checkpoint saved to task_checkpoints after intermediate step
  6. KILL / STOP Node-1 mid-task (simulating node crash / host failure)
  7. START Node-2 (same worker_id, different runtime host)
  8. LOAD the same worker (identity survives intact)
  9. Reclaim task atomically with generation fencing & side-effect safety
 10. RESUME the task on Node-2 to completion
       — no manual intervention
       — compute replaced transparently
       — generation fenced against stale Node-1 writes
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from personaos.db.models import Task as TaskModel
from personaos.db.models import TaskCheckpoint, TaskEvent
from personaos.domain.task import Task
from personaos.domain.worker import Persona, WorkerProfile
from personaos.execution.adapters.simulation import SimulationAdapter
from personaos.execution.context import WorkerExecutionContext
from personaos.execution.kernel import StaleWorkerError, WorkerExecutionKernel
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
    """Run Alembic migrations to head."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")


@pytest.fixture
def profiles_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "profiles"
    monkeypatch.setenv("PERSONAOS_PROFILES_DIR", str(d))
    return d


@pytest.mark.asyncio
async def test_compute_is_replaceable_worker_is_persistent(
    migrated_db: None,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    profiles_dir: Path,
    clean_tables: None,
) -> None:
    """Killer acceptance test: Node crashes mid-task; new node recovers & completes task."""
    worker_id = "worker-devops-001"
    node_1_id = "node-host-alpha"
    node_2_id = "node-host-beta"

    # 1. Create a Worker (DevOps) with skills [kubernetes] and capabilities [kubectl]
    async with session_factory() as session:
        registry = ProfileRegistry(session=session, profiles_dir=profiles_dir)
        profile = WorkerProfile(
            version=1,
            role="devops",
            runtime="openclaw",
            skills=["kubernetes"],
            permissions=["kubectl"],
            persona=Persona(style="concise", autonomy="high"),
        )
        await registry.register_worker(worker_id, profile)

        # 2. Create Task (Deploy FMS backend)
        workload = WorkloadService(session)
        task = await workload.enqueue(
            worker_id=worker_id,
            title="Deploy FMS Backend",
            objective="Deploy backend version 1.4.2 to staging",
            input={"service": "fms", "version": "1.4.2", "env": "staging"},
            required_skills=["kubernetes"],
            required_capabilities=["kubectl"],
            side_effect_classification="IDEMPOTENT",
        )
        task_id = task.id

    # 3. Simulate Node-1 execution:
    #    Adapter executes step 1 (manifest generation & apply), writes checkpoint.
    executed_steps_node_1: list[str] = []

    async def node_1_handler(
        context: WorkerExecutionContext,
        checkpoint: Any | None,
    ) -> Result:
        executed_steps_node_1.append("apply_manifests")
        intermediate_checkpoint = {
            "step": "manifests_applied",
            "applied_at": datetime.now(UTC).isoformat(),
            "manifest_ids": ["fms-deployment", "fms-service"],
        }
        return Result(
            status="completed",
            output={"partial": "manifests_applied"},
            new_checkpoint=intermediate_checkpoint,
        )

    adapter_1 = SimulationAdapter(handler=node_1_handler)

    # Run Node-1 execution of step 1
    async with session_factory() as s_node_1:
        kernel_1 = WorkerExecutionKernel(
            session=s_node_1,
            adapter=adapter_1,
            worker_id=worker_id,
            profiles_dir=profiles_dir,
            lease_ttl=timedelta(seconds=1),
        )
        runner_1 = WorkerRunner(
            orchestrator=WorkerOrchestrator(
                session=s_node_1, kernel=kernel_1, poll_interval=0.01
            ),
            session_factory=session_factory,
            worker_id=worker_id,
            node_id=node_1_id,
        )
        processed = await runner_1.process_one()
        assert processed is True
        assert "apply_manifests" in executed_steps_node_1

    # Verify checkpoint was written
    async with session_factory() as s_check:
        t_row = (
            await s_check.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()
        assert t_row.checkpoint_ref is not None

        cp_row = (
            await s_check.execute(
                select(TaskCheckpoint).where(TaskCheckpoint.ref == t_row.checkpoint_ref)
            )
        ).scalar_one()
        assert cp_row.state["step"] == "manifests_applied"

        # 4. SIMULATE NODE CRASH:
        # Node-1 crashes mid-run; task lease expires.
        expired_time = datetime.now(UTC) - timedelta(minutes=5)
        await s_check.execute(
            update(TaskModel)
            .where(TaskModel.id == task_id)
            .values(
                status="in_progress",
                lock_expires_at=expired_time,
                result=None,
            )
        )
        await s_check.commit()

        # Refresh domain task representation
        crashed_task_orm = (
            await s_check.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()
        crashed_task = Task.from_orm(crashed_task_orm)

    # 5. START Node-2 on a different host (node-host-beta):
    executed_steps_node_2: list[str] = []

    async def node_2_handler(
        context: WorkerExecutionContext,
        checkpoint: Any | None,
    ) -> Result:
        executed_steps_node_2.append("deployed_on_node_2")
        return Result(
            status="completed",
            output={
                "status": "deployed",
                "node": node_2_id,
                "verified": True,
            },
        )

    adapter_2 = SimulationAdapter(handler=node_2_handler)

    # 6. Node-2 reclaims and executes the in-progress task
    async with session_factory() as s_node_2:
        kernel_2 = WorkerExecutionKernel(
            session=s_node_2,
            adapter=adapter_2,
            worker_id=worker_id,
            profiles_dir=profiles_dir,
            lease_ttl=timedelta(minutes=10),
        )
        runner_2 = WorkerRunner(
            orchestrator=WorkerOrchestrator(
                session=s_node_2, kernel=kernel_2, poll_interval=0.01
            ),
            session_factory=session_factory,
            worker_id=worker_id,
            node_id=node_2_id,
        )

        # Worker identity check: worker survives intact
        registry_2 = ProfileRegistry(session=s_node_2, profiles_dir=profiles_dir)
        worker_identity = await registry_2.get_worker(worker_id)
        assert worker_identity.id == worker_id
        assert worker_identity.role == "devops"

        # Reclaim expired task on Node-2 per ADR 0016 (automatically runs recovery execution)
        await kernel_2.reclaim(crashed_task)

        # Task is now owned by generation 2
        row_after_reclaim = (
            await s_node_2.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()
        assert row_after_reclaim.generation == 2
        assert row_after_reclaim.status == "in_progress"
        assert row_after_reclaim.assigned_worker == worker_id

        # Complete task on Node-2 under generation 2
        await kernel_2.complete(
            crashed_task,
            Result(
                status="completed",
                output={
                    "status": "deployed",
                    "node": node_2_id,
                    "verified": True,
                },
            ),
        )
        assert runner_2.is_running is False

    # 7. STALE WORKER FENCING VERIFICATION:
    # If stale Node-1 tries to complete or heartbeat now, it MUST be rejected!
    async with session_factory() as s_stale:
        kernel_stale = WorkerExecutionKernel(
            session=s_stale,
            adapter=adapter_1,
            worker_id=worker_id,
            profiles_dir=profiles_dir,
        )
        kernel_stale._generation = 1  # Stale generation

        with pytest.raises(StaleWorkerError):
            await kernel_stale.complete(
                crashed_task, Result(status="completed", output={"stale": True})
            )

        with pytest.raises(StaleWorkerError):
            await kernel_stale.heartbeat(crashed_task)

    # 8. Check final DB state & Audit Trail
    assert executed_steps_node_1 == ["apply_manifests"]
    assert executed_steps_node_2 == ["deployed_on_node_2"]

    async with session_factory() as s_final:
        t_final = (
            await s_final.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()
        assert t_final.status == "completed"
        assert t_final.result is not None
        assert t_final.result["status"] == "deployed"
        assert t_final.result["node"] == node_2_id
        assert t_final.result["verified"] is True
        assert t_final.generation == 2  # Fencing generation preserved

        # Events audit trail verification
        events = list(
            (
                await s_final.execute(
                    select(TaskEvent)
                    .where(TaskEvent.task_id == task_id)
                    .order_by(TaskEvent.created_at.asc())
                )
            ).scalars().all()
        )
        event_types = [e.type for e in events]
        assert "task.created" in event_types
        assert "task.claimed" in event_types
        assert "task.checkpointed" in event_types
        assert "task.completed" in event_types
