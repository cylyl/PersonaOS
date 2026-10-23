"""Integration tests for Step 18: Persistent Worker Across Replaceable Compute.

Validates the founding PersonaOS v0.1 thesis:
  "Compute is replaceable; the worker is persistent."

Verifies:
  1. Process restart preserves worker identity, role, and active status.
  2. Reconstructing execution context preserves immutable profile snapshot version.
  3. Persisted experience (results, checkpoints, events) survives compute replacement.
  4. Replacement adapter continues task execution from durable checkpoint state.
  5. Transient adapter/process state does not leak across compute boundaries.
  6. Crash after checkpoint persistence before ACK resumes safely without lost work.
  7. Generation fencing blocks stale compute from overwriting newer execution state.
  8. Security policy evaluation and approval gates remain enforced across compute replacement.
  9. Operator review approval re-entry supports checkpoint preservation or clean reset.
 10. Ambiguous external side effects are never blindly auto-reclaimed on compute replacement.
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
from personaos.db.models import TaskEvent, WorkerNode
from personaos.domain.task import Task
from personaos.domain.worker import Persona, WorkerProfile
from personaos.execution.adapters.simulation import SimulationAdapter
from personaos.execution.context import WorkerExecutionContext
from personaos.execution.kernel import StaleWorkerError, WorkerExecutionKernel
from personaos.execution.result import Result
from personaos.execution.runner import WorkerRunner
from personaos.orchestrator.worker import WorkerOrchestrator
from personaos.registry.profile import ProfileRegistry
from personaos.workload.review import TaskReviewService
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
def profiles_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "profiles"
    monkeypatch.setenv("PERSONAOS_PROFILES_DIR", str(d))
    return d


# -----------------------------------------------------------------------------
# Scenario 1: Process Restart Preserves Worker Identity
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_restart_preserves_worker_identity_and_status(
    database_url: str,
    session_factory: async_sessionmaker[AsyncSession],
    profiles_dir: Path,
) -> None:
    """Worker identity outlives individual runner process lifecycles and node bindings."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    worker_id = "worker-persist-01"

    # Step 1: Initialize persistent worker in PostgreSQL and disk profile
    async with session_factory() as session:
        reg = ProfileRegistry(session=session, profiles_dir=profiles_dir)
        profile = WorkerProfile(
            version=1,
            role="devops",
            runtime="openclaw",
            skills=["kubernetes", "terraform"],
            permissions=["k8s.apply"],
            persona=Persona(style="concise", autonomy="high"),
        )
        await reg.register_worker(worker_id, profile)

    # Step 2: Compute Process 1 runs on node-alpha
    async with session_factory() as session_1:
        reg_1 = ProfileRegistry(session=session_1, profiles_dir=profiles_dir)
        kernel_1 = WorkerExecutionKernel(
            session=session_1,
            adapter=SimulationAdapter(),
            resolve_context=reg_1.resolve_execution_context,
            worker_id=worker_id,
            node_id="node-alpha",
        )
        runner_1 = WorkerRunner(
            orchestrator=WorkerOrchestrator(session=session_1, kernel=kernel_1),
            session_factory=session_factory,
            worker_id=worker_id,
            node_id="node-alpha",
        )
        await runner_1._startup()
        assert runner_1.is_running is True

        # Stop Process 1 cleanly
        runner_1.stop()
        await runner_1._shutdown()
        assert runner_1.is_running is False

    # Step 3: Compute Process 2 starts on node-beta for the SAME worker
    async with session_factory() as session_2:
        reg_2 = ProfileRegistry(session=session_2, profiles_dir=profiles_dir)
        kernel_2 = WorkerExecutionKernel(
            session=session_2,
            adapter=SimulationAdapter(),
            resolve_context=reg_2.resolve_execution_context,
            worker_id=worker_id,
            node_id="node-beta",
        )
        runner_2 = WorkerRunner(
            orchestrator=WorkerOrchestrator(session=session_2, kernel=kernel_2),
            session_factory=session_factory,
            worker_id=worker_id,
            node_id="node-beta",
        )
        await runner_2._startup()
        assert runner_2.is_running is True
        await runner_2._shutdown()

    # Step 4: Verify worker identity remained constant and node bindings transitioned cleanly
    async with session_factory() as session_check:
        reg_check = ProfileRegistry(session=session_check, profiles_dir=profiles_dir)
        worker = await reg_check.get_worker(worker_id)
        assert worker.id == worker_id
        assert worker.role == "devops"
        assert worker.status == "idle"

        # Check binding history
        bindings = list(
            (
                await session_check.execute(
                    select(WorkerNode)
                    .where(WorkerNode.worker_id == worker_id)
                    .order_by(WorkerNode.bound_at.asc())
                )
            )
            .scalars()
            .all()
        )
        assert len(bindings) == 2
        assert bindings[0].node_id == "node-alpha"
        assert bindings[0].unbound_at is not None
        assert bindings[0].reason == "runner_shutdown"
        assert bindings[1].node_id == "node-beta"


# -----------------------------------------------------------------------------
# Scenario 2: Execution Context Preserves Immutable Profile Snapshot Version
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconstruct_worker_context_preserves_immutable_profile_version(
    database_url: str,
    session_factory: async_sessionmaker[AsyncSession],
    profiles_dir: Path,
) -> None:
    """Reconstructing context on a replacement compute process retains the snapshot profile."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    worker_id = "worker-persist-02"

    async with session_factory() as session:
        reg = ProfileRegistry(session=session, profiles_dir=profiles_dir)
        v1 = WorkerProfile(
            version=1,
            role="developer",
            runtime="openclaw",
            skills=["python"],
            persona=Persona(autonomy="medium"),
        )
        await reg.register_worker(worker_id, v1)

        # Enqueue Task bound to v1
        wl = WorkloadService(session)
        task = await wl.enqueue(
            worker_id=worker_id,
            title="Snapshot Task",
            objective="Run with v1 configuration",
            input={},
            profile_version=1,
        )

        # Upgrade Worker to v2
        v2 = WorkerProfile(
            version=2,
            role="lead-developer",
            runtime="simulation",
            skills=["python", "architecture"],
            persona=Persona(autonomy="high"),
        )
        await reg.create_profile_version(worker_id, v2)
        await reg.activate_profile_version(worker_id, version=2)

    # Replacement compute reconstructs execution context for Task
    async with session_factory() as session_rec:
        reg_rec = ProfileRegistry(session=session_rec, profiles_dir=profiles_dir)
        ctx = await reg_rec.resolve_execution_context(task)

        # Worker's live profile version is 2
        assert ctx.worker.current_profile_version == 2
        assert ctx.worker.id == worker_id
        # Task's execution profile is strictly the immutable v1 snapshot
        assert ctx.profile.version == 1
        assert ctx.profile.role == "developer"
        assert ctx.profile.skills == ["python"]


# -----------------------------------------------------------------------------
# Scenario 3: Persistent Experience Survives Compute Replacement
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persistent_experience_survives_compute_replacement(
    database_url: str,
    session_factory: async_sessionmaker[AsyncSession],
    profiles_dir: Path,
) -> None:
    """Historical completed tasks, outputs, and event streams remain durable across restarts."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    worker_id = "worker-persist-03"

    # Step 1: Process 1 executes Task 1
    async with session_factory() as session_1:
        reg_1 = ProfileRegistry(session=session_1, profiles_dir=profiles_dir)
        profile = WorkerProfile(version=1, role="devops", runtime="simulation")
        await reg_1.register_worker(worker_id, profile)

        wl_1 = WorkloadService(session_1)
        task_1 = await wl_1.enqueue(
            worker_id=worker_id,
            title="Task 1",
            objective="Deploy cluster",
            input={"env": "prod"},
        )

        adapter_1 = SimulationAdapter(
            result=Result(status="completed", output={"cluster_id": "c-100"})
        )
        kernel_1 = WorkerExecutionKernel(
            session=session_1,
            adapter=adapter_1,
            resolve_context=reg_1.resolve_execution_context,
            worker_id=worker_id,
            node_id="node-1",
        )
        orch_1 = WorkerOrchestrator(session=session_1, kernel=kernel_1, worker_id=worker_id)
        assert await orch_1.process_one() is True

    # Process 1 terminates. Process 2 starts on replacement compute.
    async with session_factory() as session_2:
        # Query worker's completed experience from PostgreSQL
        completed_tasks = list(
            (
                await session_2.execute(
                    select(TaskModel).where(
                        TaskModel.assigned_worker == worker_id, TaskModel.status == "completed"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(completed_tasks) == 1
        assert completed_tasks[0].result == {"cluster_id": "c-100"}

        # Query audit event log for the worker
        events = list(
            (
                await session_2.execute(
                    select(TaskEvent)
                    .where(TaskEvent.task_id == task_1.id)
                    .order_by(TaskEvent.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        event_types = [e.type for e in events]
        assert "task.created" in event_types
        assert "task.claimed" in event_types
        assert "task.completed" in event_types


# -----------------------------------------------------------------------------
# Scenario 4: Replacement Adapter Continues Task from Durable Checkpoint
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replacement_adapter_continues_task_from_durable_checkpoint(
    database_url: str,
    session_factory: async_sessionmaker[AsyncSession],
    profiles_dir: Path,
) -> None:
    """Replacement compute reclaims task with resume_from_checkpoint=True
    and skips completed stages.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    worker_id = "worker-persist-04"

    # Step 1: Compute 1 claims task and persists intermediate checkpoint
    async with session_factory() as session_1:
        reg_1 = ProfileRegistry(session=session_1, profiles_dir=profiles_dir)
        profile = WorkerProfile(version=1, role="devops", runtime="simulation")
        await reg_1.register_worker(worker_id, profile)

        wl_1 = WorkloadService(session_1)
        task = await wl_1.enqueue(
            worker_id=worker_id,
            title="Multi-stage Migration",
            objective="Migrate database tables",
            input={"tables": ["users", "orders"]},
            side_effect_classification="IDEMPOTENT",
        )

        kernel_1 = WorkerExecutionKernel(
            session=session_1,
            adapter=SimulationAdapter(),
            resolve_context=reg_1.resolve_execution_context,
            worker_id=worker_id,
            node_id="node-alpha",
            lease_ttl=timedelta(seconds=1),
        )
        # Worker claims task
        await session_1.execute(
            update(TaskModel)
            .where(TaskModel.id == task.id)
            .values(
                status="in_progress",
                assigned_worker=worker_id,
                assigned_node="node-alpha",
                generation=1,
                lock_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
        await session_1.commit()
        kernel_1._generation = 1

        # Write intermediate checkpoint for stage 1
        await kernel_1.checkpoint_write(
            task,
            ref="cp-stage-1",
            state={"stage": 1, "completed_tables": ["users"]},
        )

        # Simulate abrupt Compute 1 failure: task expires in-progress
        await session_1.execute(
            update(TaskModel)
            .where(TaskModel.id == task.id)
            .values(lock_expires_at=datetime.now(UTC) - timedelta(minutes=5))
        )
        await session_1.commit()

        # Reload domain task
        crashed_row = (
            await session_1.execute(select(TaskModel).where(TaskModel.id == task.id))
        ).scalar_one()
        crashed_task = Task.from_orm(crashed_row)

    # Step 2: Compute 2 starts on replacement host (node-beta)
    received_checkpoints: list[Any] = []

    async def compute_2_handler(context: WorkerExecutionContext, checkpoint: Any | None) -> Result:
        received_checkpoints.append(checkpoint)
        assert checkpoint is not None
        assert checkpoint["stage"] == 1
        assert checkpoint["completed_tables"] == ["users"]
        # Resume directly to stage 2
        return Result(
            status="completed",
            output={
                "stage": 2,
                "completed_tables": ["users", "orders"],
                "recovered_by": "node-beta",
            },
        )

    adapter_2 = SimulationAdapter(handler=compute_2_handler)

    async with session_factory() as session_2:
        reg_2 = ProfileRegistry(session=session_2, profiles_dir=profiles_dir)
        kernel_2 = WorkerExecutionKernel(
            session=session_2,
            adapter=adapter_2,
            resolve_context=reg_2.resolve_execution_context,
            worker_id=worker_id,
            node_id="node-beta",
            lease_ttl=timedelta(minutes=5),
        )

        # Reclaim with checkpoint resumption
        await kernel_2.reclaim(crashed_task, resume_from_checkpoint=True)

        # Complete task under new generation
        await kernel_2.complete(
            crashed_task,
            Result(
                status="completed",
                output={
                    "stage": 2,
                    "completed_tables": ["users", "orders"],
                    "recovered_by": "node-beta",
                },
            ),
        )

    # Step 3: Verify execution resumed from checkpoint and finalized cleanly
    assert len(received_checkpoints) == 1
    assert received_checkpoints[0]["stage"] == 1

    async with session_factory() as session_check:
        final_row = (
            await session_check.execute(select(TaskModel).where(TaskModel.id == task.id))
        ).scalar_one()
        assert final_row.status == "completed"
        assert final_row.generation == 2
        assert final_row.checkpoint_ref == "cp-stage-1"
        assert final_row.result["recovered_by"] == "node-beta"


# -----------------------------------------------------------------------------
# Scenario 5: Transient State Does Not Leak Across Compute Boundaries
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_adapter_state_does_not_leak_into_worker_or_replacement_compute(
    database_url: str,
    session_factory: async_sessionmaker[AsyncSession],
    profiles_dir: Path,
) -> None:
    """In-memory state attached to an adapter instance is isolated from durable worker state."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    worker_id = "worker-persist-05"

    async with session_factory() as session_1:
        reg_1 = ProfileRegistry(session=session_1, profiles_dir=profiles_dir)
        profile = WorkerProfile(version=1, role="developer", runtime="simulation")
        await reg_1.register_worker(worker_id, profile)

        adapter_1 = SimulationAdapter()
        # Simulate adapter carrying internal transient state
        adapter_1.transient_cache = {"session_token": "temp-12345"}  # type: ignore[attr-defined]

    # Replacement compute
    async with session_factory() as session_2:
        reg_2 = ProfileRegistry(session=session_2, profiles_dir=profiles_dir)
        worker = await reg_2.get_worker(worker_id)
        adapter_2 = SimulationAdapter()

        # Adapter 2 has no leaked attributes from Adapter 1
        assert not hasattr(adapter_2, "transient_cache")
        # Worker identity is cleanly loaded from PostgreSQL
        assert worker.id == worker_id
        assert worker.role == "developer"


# -----------------------------------------------------------------------------
# Scenario 6: Crash After Checkpoint Persistence Resumes Safely
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crash_after_checkpoint_persistence_before_ack_is_resumed_not_lost(
    database_url: str,
    session_factory: async_sessionmaker[AsyncSession],
    profiles_dir: Path,
) -> None:
    """When a worker crashes immediately after persisting a checkpoint, work is not lost."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    worker_id = "worker-persist-06"

    async with session_factory() as session:
        reg = ProfileRegistry(session=session, profiles_dir=profiles_dir)
        profile = WorkerProfile(version=1, role="devops", runtime="simulation")
        await reg.register_worker(worker_id, profile)

        wl = WorkloadService(session)
        task = await wl.enqueue(
            worker_id=worker_id,
            title="Resilient Checkpoint Task",
            objective="Demonstrate crash after checkpoint",
            input={},
            side_effect_classification="IDEMPOTENT",
        )

        # Claim and checkpoint
        kernel = WorkerExecutionKernel(
            session=session,
            adapter=SimulationAdapter(),
            resolve_context=reg.resolve_execution_context,
            worker_id=worker_id,
            node_id="node-crash",
            lease_ttl=timedelta(seconds=1),
        )
        await session.execute(
            update(TaskModel)
            .where(TaskModel.id == task.id)
            .values(
                status="in_progress",
                assigned_worker=worker_id,
                assigned_node="node-crash",
                generation=1,
                lock_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
        await session.commit()
        kernel._generation = 1
        await kernel.checkpoint_write(task, ref="cp-persisted-01", state={"stage": "halfway_done"})

        # Simulate crash before completion (lease expiration)
        await session.execute(
            update(TaskModel)
            .where(TaskModel.id == task.id)
            .values(lock_expires_at=datetime.now(UTC) - timedelta(seconds=10))
        )
        await session.commit()

        crashed_row = (
            await session.execute(select(TaskModel).where(TaskModel.id == task.id))
        ).scalar_one()
        crashed_task = Task.from_orm(crashed_row)

    # Replacement compute resumes
    received_state: list[Any] = []

    async def recovery_step(ctx: WorkerExecutionContext, cp: Any | None) -> Result:
        received_state.append(cp)
        return Result(status="completed", output={"finished": True})

    async with session_factory() as session_rec:
        reg_rec = ProfileRegistry(session=session_rec, profiles_dir=profiles_dir)
        kernel_rec = WorkerExecutionKernel(
            session=session_rec,
            adapter=SimulationAdapter(handler=recovery_step),
            resolve_context=reg_rec.resolve_execution_context,
            worker_id=worker_id,
            node_id="node-recovery",
        )
        await kernel_rec.reclaim(crashed_task, resume_from_checkpoint=True)
        await kernel_rec.complete(
            crashed_task, Result(status="completed", output={"finished": True})
        )

    assert len(received_state) == 1
    assert received_state[0] == {"stage": "halfway_done"}


# -----------------------------------------------------------------------------
# Scenario 7: Generation Fencing Blocks Stale Compute After Replacement
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generation_fencing_blocks_stale_compute_after_replacement(
    database_url: str,
    session_factory: async_sessionmaker[AsyncSession],
    profiles_dir: Path,
) -> None:
    """Stale compute with generation N cannot write after replacement compute
    advances generation to N+1.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    worker_id = "worker-persist-07"

    async with session_factory() as session_setup:
        reg = ProfileRegistry(session=session_setup, profiles_dir=profiles_dir)
        profile = WorkerProfile(version=1, role="devops", runtime="simulation")
        await reg.register_worker(worker_id, profile)

        wl = WorkloadService(session_setup)
        task = await wl.enqueue(
            worker_id=worker_id,
            title="Fenced Task",
            objective="Prove fencing across compute replacement",
            input={},
            side_effect_classification="IDEMPOTENT",
        )

        kernel_old = WorkerExecutionKernel(
            session=session_setup,
            adapter=SimulationAdapter(),
            resolve_context=reg.resolve_execution_context,
            worker_id=worker_id,
            node_id="node-slow",
            lease_ttl=timedelta(seconds=1),
        )
        await session_setup.execute(
            update(TaskModel)
            .where(TaskModel.id == task.id)
            .values(
                status="in_progress",
                assigned_worker=worker_id,
                assigned_node="node-slow",
                generation=1,
                lock_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
        await session_setup.commit()
        kernel_old._generation = 1
        assert kernel_old._generation == 1

        # Expire lease
        await session_setup.execute(
            update(TaskModel)
            .where(TaskModel.id == task.id)
            .values(lock_expires_at=datetime.now(UTC) - timedelta(seconds=10))
        )
        await session_setup.commit()

        crashed_row = (
            await session_setup.execute(select(TaskModel).where(TaskModel.id == task.id))
        ).scalar_one()
        crashed_task = Task.from_orm(crashed_row)

    # Replacement compute reclaims (advances generation to 2)
    async with session_factory() as session_new:
        reg_new = ProfileRegistry(session=session_new, profiles_dir=profiles_dir)
        kernel_new = WorkerExecutionKernel(
            session=session_new,
            adapter=SimulationAdapter(),
            resolve_context=reg_new.resolve_execution_context,
            worker_id=worker_id,
            node_id="node-fast",
        )
        await kernel_new.reclaim(crashed_task, resume_from_checkpoint=True)
        assert kernel_new._generation == 2

    # Stale compute attempts to write checkpoint or complete under generation 1 -> FENCED
    async with session_factory() as session_stale:
        kernel_old._session = session_stale
        with pytest.raises(StaleWorkerError):
            await kernel_old.checkpoint_write(task, ref="stale-ref", state={"bad": True})

        with pytest.raises(StaleWorkerError):
            await kernel_old.complete(task, Result(status="completed", output={"stale": True}))


# -----------------------------------------------------------------------------
# Scenario 8: Security Policy Enforced Across Compute Replacement
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_security_policies_enforced_across_compute_replacement(
    database_url: str,
    session_factory: async_sessionmaker[AsyncSession],
    profiles_dir: Path,
) -> None:
    """Replacing compute backend does not bypass declarative security policy enforcement."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    worker_id = "worker-persist-08"

    async with session_factory() as session:
        reg = ProfileRegistry(session=session, profiles_dir=profiles_dir)
        profile = WorkerProfile(version=1, role="devops", runtime="simulation")
        await reg.register_worker(worker_id, profile)

        wl = WorkloadService(session)
        # Task with privileged action that standard policy denies
        task = await wl.enqueue(
            worker_id=worker_id,
            title="Destroy Prod DB",
            objective="Delete production cluster",
            input={"resource": "prod-cluster-01"},
            type="cluster:destroy",
        )

    # Compute 1 attempts execution -> Denied
    async with session_factory() as session_1:
        reg_1 = ProfileRegistry(session=session_1, profiles_dir=profiles_dir)
        kernel_1 = WorkerExecutionKernel(
            session=session_1,
            adapter=SimulationAdapter(),
            resolve_context=reg_1.resolve_execution_context,
            worker_id=worker_id,
            node_id="node-1",
        )
        res_1 = await kernel_1.execute(task)
        assert res_1.status == "failed"
        assert "Security policy denied execution" in str(res_1.error)

    # Reset task to queued to test replacement compute
    async with session_factory() as session_reset:
        await session_reset.execute(
            update(TaskModel)
            .where(TaskModel.id == task.id)
            .values(status="queued", assigned_node=None)
        )
        await session_reset.commit()

    # Compute 2 on different node attempts execution -> Still Denied
    async with session_factory() as session_2:
        reg_2 = ProfileRegistry(session=session_2, profiles_dir=profiles_dir)
        kernel_2 = WorkerExecutionKernel(
            session=session_2,
            adapter=SimulationAdapter(),
            resolve_context=reg_2.resolve_execution_context,
            worker_id=worker_id,
            node_id="node-2",
        )
        res_2 = await kernel_2.execute(task)
        assert res_2.status == "failed"
        assert "Security policy denied execution" in str(res_2.error)


# -----------------------------------------------------------------------------
# Scenario 9: Review Service Approval Supports Checkpoint Preservation
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operator_review_approval_preserves_checkpoint_for_resumption(
    database_url: str,
    session_factory: async_sessionmaker[AsyncSession],
    profiles_dir: Path,
) -> None:
    """Operator approval can re-queue with checkpoint preserved or explicitly cleared."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    worker_id = "worker-persist-09"

    async with session_factory() as session:
        reg = ProfileRegistry(session=session, profiles_dir=profiles_dir)
        profile = WorkerProfile(version=1, role="devops", runtime="simulation")
        await reg.register_worker(worker_id, profile)

        wl = WorkloadService(session)
        task = await wl.enqueue(
            worker_id=worker_id,
            title="Review Checkpoint Task",
            objective="Demonstrate review re-entry",
            input={},
        )

        # Insert a checkpoint and put task in review
        await session.execute(
            text(
                "INSERT INTO task_checkpoints (ref, task_id, state) "
                "VALUES ('cp-rev-100', :tid, '{\"checkpoint\": \"data\"}'::jsonb)"
            ),
            {"tid": task.id},
        )
        await session.execute(
            update(TaskModel)
            .where(TaskModel.id == task.id)
            .values(
                status="review",
                checkpoint_ref="cp-rev-100",
                generation=1,
            )
        )
        await session.commit()

    # Operator approves with resume_from_checkpoint=True (default)
    async with session_factory() as session_app:
        review_svc = TaskReviewService(session_app)
        requeued = await review_svc.approve(
            task_id=task.id,
            expected_generation=1,
            operator_id="operator:alice",
            reason="Approved with checkpoint preserved",
            resume_from_checkpoint=True,
        )
        await session_app.commit()
        assert requeued.status == "queued"
        assert requeued.checkpoint_ref == "cp-rev-100"
        assert requeued.generation == 2

    # Put task back in review under generation 2
    async with session_factory() as session_rev2:
        await session_rev2.execute(
            update(TaskModel).where(TaskModel.id == task.id).values(status="review")
        )
        await session_rev2.commit()

    # Operator approves with resume_from_checkpoint=False (clean reset)
    async with session_factory() as session_app2:
        review_svc_2 = TaskReviewService(session_app2)
        requeued_clean = await review_svc_2.approve(
            task_id=task.id,
            expected_generation=2,
            operator_id="operator:alice",
            reason="Approved with fresh start",
            resume_from_checkpoint=False,
        )
        await session_app2.commit()
        assert requeued_clean.status == "queued"
        assert requeued_clean.checkpoint_ref is None
        assert requeued_clean.generation == 3


# -----------------------------------------------------------------------------
# Scenario 10: Ambiguous Side Effects Blocked from Auto-Reclaim
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ambiguous_side_effects_blocked_from_auto_reclaim_on_compute_replacement(
    database_url: str,
    session_factory: async_sessionmaker[AsyncSession],
    profiles_dir: Path,
) -> None:
    """Tasks with side effects are never auto-reclaimed;
    replacement compute routes them to review.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    worker_id = "worker-persist-10"

    async with session_factory() as session:
        reg = ProfileRegistry(session=session, profiles_dir=profiles_dir)
        profile = WorkerProfile(version=1, role="devops", runtime="simulation")
        await reg.register_worker(worker_id, profile)

        wl = WorkloadService(session)
        task = await wl.enqueue(
            worker_id=worker_id,
            title="Non-idempotent Payment Task",
            objective="Charge credit card",
            input={"amount": 100},
            side_effect_classification="HAS_SIDE_EFFECTS",
        )

        # Simulate crash mid-execution
        await session.execute(
            update(TaskModel)
            .where(TaskModel.id == task.id)
            .values(
                status="in_progress",
                lock_expires_at=datetime.now(UTC) - timedelta(minutes=10),
            )
        )
        await session.commit()

    # Replacement compute orchestrator attempts process_one()
    async with session_factory() as session_orch:
        reg_orch = ProfileRegistry(session=session_orch, profiles_dir=profiles_dir)
        kernel_orch = WorkerExecutionKernel(
            session=session_orch,
            adapter=SimulationAdapter(),
            resolve_context=reg_orch.resolve_execution_context,
            worker_id=worker_id,
            node_id="node-replacement",
        )
        orch = WorkerOrchestrator(session=session_orch, kernel=kernel_orch, worker_id=worker_id)
        processed = await orch.process_one()
        assert processed is True  # Handled stale task by routing to review

    # Verify task routed to review queue rather than executing duplicate charge
    async with session_factory() as session_check:
        row = (
            await session_check.execute(select(TaskModel).where(TaskModel.id == task.id))
        ).scalar_one()
        assert row.status == "review"
        assert "auto-reclaim blocked (HAS_SIDE_EFFECTS)" in str(row.last_error)
