"""E2E Real Process Crash Recovery Validation (Step 13b).

Validates the full killer resilience property against real OS-level subprocess
crashes (SIGKILL), unclosed sockets, ungraceful node failure, generation fencing,
and external side-effect duplication dynamics.

Per Step 13b / ADR 0016 / VIKUNJA.md:
  1. Worker runs in an independent OS process, claims a task, establishes a lease.
  2. Worker writes an intermediate checkpoint to PostgreSQL.
  3. Parent test sends SIGKILL to terminate the worker abruptly (no shutdown hooks).
  4. Lease expires in PostgreSQL.
  5. Replacement worker on a new node discovers and reclaims the task.
  6. Generation fencing blocks stale generation writes.
  7. Side-effect duplication dynamics are explicitly verified and documented.
  8. Non-idempotent tasks route to review without re-executing side effects.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from personaos.db.models import Task as TaskModel
from personaos.db.models import TaskCheckpoint, WorkerNode
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
CRASH_WORKER_SCRIPT = Path(__file__).parent / "crash_worker.py"


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
async def test_real_os_process_sigkill_crash_and_idempotent_reclaim(
    migrated_db: None,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    database_url: str,
    profiles_dir: Path,
    tmp_path: Path,
    clean_tables: None,
) -> None:
    """Validate crash recovery across real OS process termination via SIGKILL.

    Scenario:
      1. Register worker 'worker-infra-001' on 'node-crash-alpha'.
      2. Enqueue an IDEMPOTENT task with max_retries=3 and 1.5s lease TTL.
      3. Launch independent OS subprocess running crash_worker.py.
      4. Subprocess claims task, writes checkpoint 'cp-crash-001', logs side effect,
         emits READY_FOR_KILL, and awaits execution.
      5. Parent test verifies in-progress DB state, then issues SIGKILL (-9).
      6. Parent test verifies process died ungracefully without clearing active binding.
      7. Lease expires in PostgreSQL.
      8. Replacement worker on 'node-recovery-beta' automatically reclaims task,
         clears checkpoint_ref to NULL per ADR 0016 AC #8, and executes recovery.
      9. Stale worker write attempts under generation 1 are fenced with StaleWorkerError.
     10. Verify and document side-effect duplication dynamics.
    """
    worker_id = "worker-infra-001"
    node_1_id = "node-crash-alpha"
    node_2_id = "node-recovery-beta"
    side_effect_file = tmp_path / "external_side_effects.log"

    # 1. Register worker
    async with session_factory() as session:
        registry = ProfileRegistry(session=session, profiles_dir=profiles_dir)
        profile = WorkerProfile(
            version=1,
            role="infra",
            runtime="openclaw",
            skills=["terraform"],
            permissions=["aws"],
            persona=Persona(style="concise", autonomy="high"),
        )
        await registry.register_worker(worker_id, profile)

        # 2. Enqueue an IDEMPOTENT task with short lease TTL (1.5s)
        workload = WorkloadService(session)
        task = await workload.enqueue(
            worker_id=worker_id,
            title="Provision VPC",
            objective="Deploy staging VPC",
            input={"vpc_cidr": "10.0.0.0/16"},
            side_effect_classification="IDEMPOTENT",
            max_retries=3,
        )
        task_id = task.id

    # 3. Launch real OS subprocess
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{REPO_ROOT / 'src'}:{env.get('PYTHONPATH', '')}"
    env["DATABASE_URL"] = database_url

    proc = subprocess.Popen(
        [
            sys.executable,
            "-u",
            str(CRASH_WORKER_SCRIPT),
            database_url,
            worker_id,
            node_1_id,
            task_id,
            str(profiles_dir),
            str(side_effect_file),
            "1.5",  # 1.5 seconds lease TTL
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    # 4. Wait for subprocess to establish lease, write checkpoint, and notify
    assert proc.stdout is not None
    ready = False
    for _ in range(50):
        line = await asyncio.to_thread(proc.stdout.readline)
        if "READY_FOR_KILL" in line:
            ready = True
            break
        if not line and proc.poll() is not None:
            break
    assert ready, (
        f"Worker failed to initialize; stderr: {proc.stderr.read() if proc.stderr else ''}"
    )

    # Verify state in DB while worker is alive in-flight
    async with session_factory() as s_check:
        t_live = (
            await s_check.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()
        assert t_live.status == "in_progress"
        assert t_live.generation == 1
        assert t_live.assigned_worker == worker_id
        assert t_live.checkpoint_ref == "cp-crash-001"
        assert t_live.lock_expires_at is not None

        # Checkpoint record exists
        cp_live = (
            await s_check.execute(
                select(TaskCheckpoint).where(TaskCheckpoint.ref == "cp-crash-001")
            )
        ).scalar_one()
        assert cp_live.state["step"] == "phase_1_done"

        # WorkerNode binding is active (unbound_at is NULL)
        node_binding = (
            await s_check.execute(
                select(WorkerNode).where(WorkerNode.node_id == node_1_id)
            )
        ).scalar_one()
        assert node_binding.unbound_at is None

    # Verify 1 side-effect occurred before crash
    side_effects_before_kill = side_effect_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(side_effects_before_kill) == 1
    assert "step1" in side_effects_before_kill[0]

    # 5. KILL WORKER SUBPROCESS ABRUPTLY VIA SIGKILL (Simulating OS / host crash)
    os.kill(proc.pid, signal.SIGKILL)
    ret = await asyncio.to_thread(proc.wait)
    assert ret == -signal.SIGKILL, f"Expected returncode -9, got {ret}"

    # Verify that ungraceful termination did NOT clean up bindings
    async with session_factory() as s_after_kill:
        binding_post_kill = (
            await s_after_kill.execute(
                select(WorkerNode).where(WorkerNode.node_id == node_1_id)
            )
        ).scalar_one()
        assert binding_post_kill.unbound_at is None, (
            "SIGKILL must not permit graceful worker_nodes unbinding"
        )

        t_post_kill = (
            await s_after_kill.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()
        assert t_post_kill.status == "in_progress"
        assert t_post_kill.generation == 1

    # 6. Wait for lease to expire (1.5s TTL -> sleep 1.8s)
    await asyncio.sleep(1.8)

    # 7. Start Replacement Worker on Node-2 and run automated recovery
    recovery_side_effects: list[str] = []

    async def recovery_handler(
        context: WorkerExecutionContext,
        checkpoint: Any | None,
    ) -> Result:
        # Checkpoint is None because ADR 0016 AC #8 clears checkpoint_ref on reclaim
        assert checkpoint is None, "ADR 0016 AC #8 mandates recovery starts with checkpoint=None"
        recovery_side_effects.append("recovered_vpc_created")
        with open(side_effect_file, "a", encoding="utf-8") as f:
            f.write(f"side_effect_attempt:{context.task.id}:recovery_complete\n")
        return Result(
            status="completed",
            output={"vpc_id": "vpc-0a1b2c3d", "node": node_2_id, "recovered": True},
        )

    recovery_adapter = SimulationAdapter(handler=recovery_handler)

    async with session_factory() as s_rec:
        kernel_rec = WorkerExecutionKernel(
            session=s_rec,
            adapter=recovery_adapter,
            worker_id=worker_id,
            profiles_dir=profiles_dir,
            lease_ttl=timedelta(minutes=10),
            session_factory=session_factory,
        )
        orchestrator_rec = WorkerOrchestrator(
            session=s_rec,
            kernel=kernel_rec,
            poll_interval=0.05,
        )
        runner_rec = WorkerRunner(
            orchestrator=orchestrator_rec,
            session_factory=session_factory,
            worker_id=worker_id,
            node_id=node_2_id,
        )

        # Runner startup supersedes the crashed node-1 binding (FINDING-04)
        await runner_rec._startup()

        # Run orchestrator iteration: automatically discovers and recovers stale task (FINDING-03)
        processed = await orchestrator_rec.process_one()
        assert processed is True
        assert orchestrator_rec.metrics.tasks_processed == 1

        # Check DB state after recovery
        t_recovered = (
            await s_rec.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()
        assert t_recovered.status == "completed"
        assert t_recovered.generation == 2
        assert t_recovered.retry_count == 1
        assert t_recovered.checkpoint_ref is None
        assert t_recovered.lock_expires_at is None
        assert t_recovered.result == {
            "vpc_id": "vpc-0a1b2c3d",
            "node": node_2_id,
            "recovered": True,
        }

        # 8. Generation Fencing: Verify stale Node-1 worker (generation 1) is rejected
        stale_kernel = WorkerExecutionKernel(
            session=s_rec,
            adapter=SimulationAdapter(),
            worker_id=worker_id,
            profiles_dir=profiles_dir,
        )
        stale_kernel._generation = 1  # Stale generation
        stale_task = Task.from_orm(t_recovered)

        with pytest.raises(StaleWorkerError):
            await stale_kernel.complete(
                stale_task,
                Result(status="completed", output={"stale_write": "illegal"}),
            )

        with pytest.raises(StaleWorkerError):
            await stale_kernel.heartbeat(stale_task)

        # 9. Verify and document side-effect duplication dynamics
        all_side_effects = side_effect_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(all_side_effects) == 2, (
            "Because crash occurred after step 1 side effect, recovery restarted from scratch, "
            "producing 2 side effect log entries across both process attempts."
        )

        # Clean shutdown of runner_2
        await runner_rec._shutdown()


@pytest.mark.asyncio
async def test_real_os_process_sigkill_crash_routes_non_idempotent_task_to_review(
    migrated_db: None,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    database_url: str,
    profiles_dir: Path,
    tmp_path: Path,
    clean_tables: None,
) -> None:
    """Validate that crashed non-idempotent tasks route to 'review'
    without side-effect re-execution.

    Safety Gate Proof:
      When a task has side_effect_classification='HAS_SIDE_EFFECTS' and crashes,
      auto-reclaim is BLOCKED to avoid external side-effect duplication.
      The replacement worker detects the expired lease and fences it directly into
      the operator review queue.
    """
    worker_id = "worker-billing-001"
    node_1_id = "node-crash-gamma"
    side_effect_file = tmp_path / "billing_side_effects.log"

    # 1. Register worker
    async with session_factory() as session:
        registry = ProfileRegistry(session=session, profiles_dir=profiles_dir)
        profile = WorkerProfile(
            version=1,
            role="billing",
            runtime="openclaw",
            skills=["payments"],
            permissions=["stripe"],
            persona=Persona(style="formal", autonomy="low"),
        )
        await registry.register_worker(worker_id, profile)

        # 2. Enqueue task with HAS_SIDE_EFFECTS
        workload = WorkloadService(session)
        task = await workload.enqueue(
            worker_id=worker_id,
            title="Charge Credit Card",
            objective="Process subscription charge $49.00",
            input={"amount_cents": 4900, "customer": "cus_999"},
            side_effect_classification="HAS_SIDE_EFFECTS",
            max_retries=3,
        )
        task_id = task.id

    # 3. Launch worker subprocess
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{REPO_ROOT / 'src'}:{env.get('PYTHONPATH', '')}"
    env["DATABASE_URL"] = database_url

    proc = subprocess.Popen(
        [
            sys.executable,
            "-u",
            str(CRASH_WORKER_SCRIPT),
            database_url,
            worker_id,
            node_1_id,
            task_id,
            str(profiles_dir),
            str(side_effect_file),
            "1.5",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    # 4. Wait for worker to claim task, write checkpoint, and execute side effect
    assert proc.stdout is not None
    ready = False
    for _ in range(50):
        line = await asyncio.to_thread(proc.stdout.readline)
        if "READY_FOR_KILL" in line:
            ready = True
            break
        if not line and proc.poll() is not None:
            break
    assert ready, (
        f"Worker failed to initialize; stderr: {proc.stderr.read() if proc.stderr else ''}"
    )

    # Verify 1 side-effect executed before crash
    side_effects = side_effect_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(side_effects) == 1

    # 5. KILL SUBPROCESS VIA SIGKILL
    os.kill(proc.pid, signal.SIGKILL)
    await asyncio.to_thread(proc.wait)

    # 6. Wait for lease to expire
    await asyncio.sleep(1.8)

    # 7. Replacement Worker runs orchestrator
    async with session_factory() as s_rec:
        kernel_rec = WorkerExecutionKernel(
            session=s_rec,
            adapter=SimulationAdapter(),
            worker_id=worker_id,
            profiles_dir=profiles_dir,
            session_factory=session_factory,
        )
        orchestrator_rec = WorkerOrchestrator(
            session=s_rec,
            kernel=kernel_rec,
            poll_interval=0.05,
        )

        # Automated maintenance discovers stale HAS_SIDE_EFFECTS task and routes to review
        processed = await orchestrator_rec.process_one()
        assert processed is True

        # Check DB state: safely in 'review', generation=2, lock cleared
        t_review = (
            await s_rec.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()
        assert t_review.status == "review"
        assert t_review.generation == 2
        assert t_review.lock_expires_at is None
        assert "side-effect safety" in (t_review.last_error or "")

        # Verify NO secondary side-effect executed
        side_effects_after = side_effect_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(side_effects_after) == 1, (
            "Non-idempotent task must NEVER re-execute side effects automatically after a crash"
        )
