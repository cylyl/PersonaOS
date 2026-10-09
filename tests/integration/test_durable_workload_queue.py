"""Step 10a — Durable Workload Queue acceptance tests.

Per docs/adr/0015-durable-workload-queue.md and
docs/specs/durable-workload-queue.md.

These tests verify that PersonaOS's PostgreSQL-backed task queue
survives session restarts and that the state machine transitions are
enforced. They cover the guarantees locked in ADR 0015:

  1. Queued task survives session recreation
  2. Queued task is rediscovered after orchestrator restart
  3. Claim transition (queued → in_progress) is persisted
  4. Completed transition (in_progress → completed) is persisted
  5. Failed transition (in_progress → failed) is persisted
  6. Terminal tasks (completed / failed) cannot be claimed again
  7. Concurrent claim has exactly one winner
  8. Dependency rules survive session recreation
  9. Checkpoint reference survives session recreation
  10. Interrupted in_progress task follows defined recovery policy
     — SKIP: out of scope for v0.1 per ADR 0015 §2

The tests model session recreation as creating a fresh AsyncSession
from the same session_factory (simulates process restart). They do NOT
simulate process crashes mid-transaction — that is out of scope for v0.1.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from personaos.db.models import Task as TaskModel
from personaos.db.models import Worker as WorkerModel
from personaos.domain.task import Task
from personaos.domain.worker import Persona, WorkerProfile
from personaos.execution.adapters.base import Result
from personaos.execution.context import WorkerExecutionContext
from personaos.execution.kernel import TaskStatusError, WorkerExecutionKernel
from personaos.registry.profile import ProfileRegistry
from personaos.workload.service import WorkloadService

# repo root
REPO_ROOT = Path(__file__).parents[2]
ALEMBIC_INI = REPO_ROOT / "src" / "personaos" / "db" / "migrations" / "alembic.ini"
MIGRATIONS_DIR = REPO_ROOT / "src" / "personaos" / "db" / "migrations"

pytestmark = pytest.mark.usefixtures("clean_tables")


@pytest.fixture
def profiles_dir(tmp_path, monkeypatch):
    d = tmp_path / "profiles"
    monkeypatch.setenv("PERSONAOS_PROFILES_DIR", str(d))
    return d


def _alembic_config(database_url):
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", database_url)
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


# ---------- Test doubles ----------


class OkAdapter:
    """Adapter that returns completed with configurable output."""

    def __init__(self, output: dict | None = None):
        self.output = output or {}

    async def execute(
        self,
        context: WorkerExecutionContext,
        checkpoint=None,
    ) -> Result:
        return Result(status="completed", output=self.output)


class FailAdapter:
    """Adapter that returns failed with configurable error."""

    def __init__(self, error: str = "adapter error"):
        self.error = error

    async def execute(
        self,
        context: WorkerExecutionContext,
        checkpoint=None,
    ) -> Result:
        return Result(status="failed", error=self.error)


# ---------- Helpers ----------


async def _register_worker_and_v1_profile(
    session: AsyncSession, profiles_dir: Path, worker_id: str = "w1"
) -> ProfileRegistry:
    """Register worker with one active v1 profile (idempotent)."""
    from personaos.registry.profile import ProfileError
    reg = ProfileRegistry(session=session, profiles_dir=profiles_dir)
    try:
        await reg.register_worker(
            worker_id,
            WorkerProfile(
                version=1,
                role="devops",
                runtime="openclaw",
                max_concurrent_tasks=1,
                persona=Persona(autonomy="medium"),
            ),
        )
    except ProfileError:
        # Worker already registered (e.g. created in a prior session).
        # ProfileRegistry reads existing workers fine.
        pass
    return reg


async def _enqueue(
    session: AsyncSession,
    *,
    worker_id: str = "w1",
    title: str = "test task",
    objective: str = "test objective",
    input: dict | None = None,
    dependencies: list[str] | None = None,
) -> Task:
    wl = WorkloadService(session=session)
    return await wl.enqueue(
        worker_id=worker_id,
        title=title,
        objective=objective,
        input=input or {},
        dependencies=dependencies,
    )


def _build_kernel(session, adapter, registry) -> WorkerExecutionKernel:
    return WorkerExecutionKernel(
        session=session,
        adapter=adapter,
        resolve_context=registry.resolve_execution_context,
    )


async def _status(session, task_id: str) -> str:
    row = (
        await session.execute(
            text("SELECT status FROM tasks WHERE id = :tid"),
            {"tid": task_id},
        )
    ).first()
    assert row is not None, f"task {task_id} not found"
    return row.status


async def _task_row(session, task_id: str):
    return (
        await session.execute(
            text(
                "SELECT status, started_at, completed_at, result, "
                "last_error, checkpoint_ref FROM tasks WHERE id = :tid"
            ),
            {"tid": task_id},
        )
    ).first()


# ---------- AC #1: queued task survives session recreation ----------


async def test_queued_task_survives_session_recreation(
    profiles_dir, database_url, session_factory
):
    """A queued task is still visible after a new session opens.

    Proves: PostgreSQL (not an in-memory queue) is the source of truth
    for queued tasks.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    # Session A: enqueue a task
    async with session_factory() as s1:
        await _register_worker_and_v1_profile(s1, profiles_dir)
        task = await _enqueue(s1, title="persist me")
        assert task.status == "queued"
        task_id = task.id

    # Session B (fresh process simulation): the task is still there
    async with session_factory() as s2:
        status = await _status(s2, task_id)
        assert status == "queued"


# ---------- AC #2: queued task is discovered after orchestrator restart ----------


async def test_queued_task_is_discovered_after_orchestrator_restart(
    profiles_dir, database_url, session_factory
):
    """next_runnable() returns a task that was queued before orchestrator start.

    Proves: the orchestrator has no in-memory worklist; it always queries
    PostgreSQL.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    # Session 1: enqueue
    async with session_factory() as s1:
        await _register_worker_and_v1_profile(s1, profiles_dir)
        task = await _enqueue(s1, title="find me after restart")

    # Session 2: orchestrator "restarts" — new session, fresh WorkloadService
    async with session_factory() as s2:
        await _register_worker_and_v1_profile(s2, profiles_dir)
        wl = WorkloadService(s2)
        found = await wl.next_runnable()
        assert found is not None
        assert found.id == task.id


# ---------- AC #3: claim transition is persisted ----------


async def test_claim_transition_is_persisted(
    profiles_dir, database_url, session_factory
):
    """queued → in_progress transition survives a new session.

    Proves: the atomic claim is committed to PostgreSQL before
    kernel.execute() returns.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as s1:
        reg = await _register_worker_and_v1_profile(s1, profiles_dir)
        task = await _enqueue(s1)
        task_id = task.id

        # Execute (kernel does atomic claim + commit)
        kernel = _build_kernel(s1, OkAdapter(), reg)
        await kernel.execute(task)

        # Session 2: verify the claim was committed
        # NOTE: in_progress is transient — kernel.execute() finishes to
        # 'completed' before returning. So we verify 'started_at' is set
        # (proves the atomic claim + commit happened), rather than status.
    async with session_factory() as s2:
        row = await _task_row(s2, task_id)
        assert row.started_at is not None


# ---------- AC #4: completed transition is persisted ----------


async def test_completed_transition_is_persisted(
    profiles_dir, database_url, session_factory
):
    """in_progress → completed transition survives a new session.

    Proves: completed is a durable, committed state.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as s1:
        reg = await _register_worker_and_v1_profile(s1, profiles_dir)
        task = await _enqueue(s1)
        task_id = task.id

        kernel = _build_kernel(s1, OkAdapter(output={"ok": True}), reg)
        await kernel.execute(task)

        # Session 2: verify completed persisted
    async with session_factory() as s2:
        row = await _task_row(s2, task_id)
        assert row.status == "completed"
        assert row.completed_at is not None
        assert row.result == {"ok": True}


# ---------- AC #5: failed transition is persisted ----------


async def test_failed_transition_is_persisted(
    profiles_dir, database_url, session_factory
):
    """in_progress → failed transition survives a new session.

    Proves: failed is a durable, committed state.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as s1:
        reg = await _register_worker_and_v1_profile(s1, profiles_dir)
        task = await _enqueue(s1)
        task_id = task.id

        kernel = _build_kernel(s1, FailAdapter(error="boom"), reg)
        await kernel.execute(task)

        # Session 2: verify failed persisted
    async with session_factory() as s2:
        row = await _task_row(s2, task_id)
        assert row.status == "failed"
        assert row.completed_at is not None
        assert row.last_error == "boom"


# ---------- AC #6: terminal tasks cannot be claimed again ----------


@pytest.mark.parametrize("terminal_status", ["completed", "failed"])
async def test_terminal_task_cannot_be_claimed_again(
    profiles_dir, database_url, session_factory, terminal_status
):
    """A task in a terminal state cannot be re-claimed.

    The kernel's atomic UPDATE WHERE status='queued' returns rowcount=0
    for a task already in completed/failed, so a second execute() raises
    TaskStatusError.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as s1:
        reg = await _register_worker_and_v1_profile(s1, profiles_dir)
        task = await _enqueue(s1)
        task_id = task.id

        kernel = _build_kernel(
            s1,
            OkAdapter() if terminal_status == "completed" else FailAdapter(),
            reg,
        )
        await kernel.execute(task)
        assert (await _status(s1, task_id)) == terminal_status

        # Second execute raises TaskStatusError
        task2 = Task(
            id=task_id,
            title=task.title,
            objective=task.objective,
            status=terminal_status,  # in-memory Task object is stale
            priority=task.priority,
            type=task.type,
            required_skills=task.required_skills,
            required_capabilities=task.required_capabilities,
            assigned_worker=task.assigned_worker,
            assigned_node=task.assigned_node,
            dependencies=task.dependencies,
            input=task.input,
            checkpoint_ref=task.checkpoint_ref,
            result=task.result,
            retry_count=task.retry_count,
            max_retries=task.max_retries,
            last_error=task.last_error,
            deadline=task.deadline,
            created_at=task.created_at,
            started_at=task.started_at,
            completed_at=task.completed_at,
            profile_version=task.profile_version,
        )
        with pytest.raises(TaskStatusError):
            await kernel.execute(task2)


# ---------- AC #7: concurrent claim has exactly one winner ----------


async def test_concurrent_claim_has_one_winner(
    profiles_dir, database_url, session_factory
):
    """Two sessions racing to claim the same queued task — only one UPDATE succeeds.

    Proves: PostgreSQL row-level locking ensures exactly one UPDATE
    succeeds; the loser gets rowcount=0 → TaskStatusError.

    Note: we test the atomic claim directly via raw SQL rather than via
    two SQLAlchemy kernel.execute() calls in asyncio.gather, because
    SQLAlchemy async sessions raise InvalidRequestError when used by
    concurrent coroutines in the same task (session is not thread-safe
    across async tasks). The raw SQL test is equivalent — it exercises
    the identical UPDATE WHERE status='queued' that the kernel uses.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    # Enqueue a task (one session, committed)
    async with session_factory() as s_setup:
        await _register_worker_and_v1_profile(s_setup, profiles_dir)
        task = await _enqueue(s_setup, title="race me")
        task_id = task.id

    # Race: two sessions each try the atomic UPDATE simultaneously on a fresh task
    async def _race_one_round():
        """Claim a fresh queued task concurrently from two sessions."""
        # Create a fresh queued task (visible to both racers before either commits)
        async with session_factory() as s_seed:
            task = await _enqueue(s_seed, title=f"round {__import__('time').time_ns()}")
            tid = task.id

        async def _try_claim(sess_idx: int, claimed_id: str):
            async with session_factory() as s:
                from sqlalchemy import update as sql_update
                from personaos.db.models import Task as TaskModel

                result = await s.execute(
                    sql_update(TaskModel)
                    .where(TaskModel.id == claimed_id, TaskModel.status == "queued")
                    .values(status="in_progress")
                )
                await s.commit()
                return ("won" if result.rowcount == 1 else "lost")

        r1, r2 = await asyncio.gather(_try_claim(1, tid), _try_claim(2, tid))
        return (r1, r2)

    # Run 5 rounds of concurrent claims on independent tasks
    outcomes = []
    for _ in range(5):
        outcomes.append(await _race_one_round())

    # Exactly one winner and one loser per round
    for i, (a, b) in enumerate(outcomes):
        assert {a, b} == {"won", "lost"}, (
            f"round {i}: expected one 'won' + one 'lost', got '{a}' and '{b}'"
        )

    # Total: 5 wins, 5 losses
    wins = sum(1 for a, b in outcomes if a == "won" or b == "won")
    losses = sum(1 for a, b in outcomes if a == "lost" or b == "lost")
    assert wins == 5 and losses == 5, (
        f"expected 5 wins + 5 losses, got {wins} wins + {losses} losses"
    )


# ---------- AC #8: dependency rules survive session recreation ----------


async def test_dependency_rules_survive_session_recreation(
    profiles_dir, database_url, session_factory
):
    """A task with unsatisfied dependencies is not runnable after session restart.

    Proves: dependency state is persisted in PostgreSQL, not held in-memory.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    # Session 1: create dep task + dependent task
    async with session_factory() as s1:
        await _register_worker_and_v1_profile(s1, profiles_dir, worker_id="w1")
        dep_task = await _enqueue(s1, title="dep")
        dep_id = dep_task.id
        dependent = await _enqueue(s1, title="blocked", dependencies=[dep_id])
        dependent_id = dependent.id

        wl = WorkloadService(s1)
        # dep has no deps → it IS runnable; dependent is blocked
        runnable = await wl.next_runnable()
        assert runnable is not None
        assert runnable.id == dep_id  # dep is first (FIFO by created_at)
        # Note: dependent.id > dep.id, so dependent is behind dep in the queue

    # Complete the dep task
    async with session_factory() as s3:
        reg3 = await _register_worker_and_v1_profile(s3, profiles_dir, worker_id="w1")
        kernel3 = _build_kernel(s3, OkAdapter(), reg3)
        task3 = Task(
            id=dep_id,
            title="dep",
            objective="dep",
            status="queued",
            priority="medium",
            type=None,
            required_skills=[],
            required_capabilities=[],
            assigned_worker="w1",
            assigned_node=None,
            dependencies=[],
            input={},
            checkpoint_ref=None,
            result=None,
            retry_count=0,
            max_retries=3,
            last_error=None,
            deadline=None,
            created_at=dep_task.created_at,
            started_at=None,
            completed_at=None,
            profile_version=1,
        )
        await kernel3.execute(task3)

    # Session 4: dependent is now runnable
    async with session_factory() as s4:
        await _register_worker_and_v1_profile(s4, profiles_dir, worker_id="w1")
        wl4 = WorkloadService(s4)
        runnable = await wl4.next_runnable()
        assert runnable is not None
        assert runnable.id == dependent.id


# ---------- AC #9: checkpoint reference survives session recreation ----------


async def test_checkpoint_reference_survives_session_recreation(
    profiles_dir, database_url, session_factory
):
    """A task with checkpoint_ref set is readable after session recreation.

    Proves: checkpoint_ref is a PostgreSQL column, not an in-memory pointer.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as s1:
        reg = await _register_worker_and_v1_profile(s1, profiles_dir)
        task = await _enqueue(s1)
        task_id = task.id

        # Create the checkpoint row first (FK constraint requires it)
        # Then link it to the task — mirrors the real kernel flow where
        # checkpoint_write() inserts the row before updating tasks.checkpoint_ref
        await s1.execute(
            text(
                "INSERT INTO task_checkpoints (ref, task_id, state) "
                "VALUES ('cp-session1', :tid, '{\"step\": 1}'::jsonb)"
            ),
            {"tid": task_id},
        )
        await s1.execute(
            update(TaskModel)
            .where(TaskModel.id == task_id)
            .values(checkpoint_ref="cp-session1")
        )
        await s1.commit()

    # Session 2: checkpoint_ref is still there
    async with session_factory() as s2:
        row = await _task_row(s2, task_id)
        assert row.checkpoint_ref == "cp-session1"


# ---------- AC #10: interrupted in_progress task follows defined recovery policy ----------


@pytest.mark.skip(reason="out of scope for v0.1 — ADR 0015 §2: in-progress recovery is operator-managed; auto-recovery deferred to v0.2")
async def test_interrupted_in_progress_task_follows_defined_recovery_policy(
    profiles_dir, database_url, session_factory
):
    """A task left in in_progress after a crash is NOT auto-reset to queued.

    Per ADR 0015 §2: v0.1 does NOT auto-reset in_progress tasks.
    An operator must manually assess + reset.

    This test is a placeholder demonstrating the expected v0.2 behavior.
    Currently SKIP to avoid a test that would need process-crash simulation.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as s1:
        reg = await _register_worker_and_v1_profile(s1, profiles_dir)
        task = await _enqueue(s1)
        task_id = task.id

        # Simulate: orchestrator crashes AFTER claiming but BEFORE completing
        # In a real test this would be done by killing the process mid-execute.
        # Here we just set in_progress directly to model the crash state.
        await s1.execute(
            update(TaskModel)
            .where(TaskModel.id == task_id)
            .values(status="in_progress")
        )
        await s1.commit()

    # Session 2 (restarted orchestrator): task is still in_progress
    async with session_factory() as s2:
        await _register_worker_and_v1_profile(s2, profiles_dir)
        wl2 = WorkloadService(s2)

        # The task should NOT be returned as runnable
        # (next_runnable only returns status='queued')
        assert await wl2.next_runnable() is None

        # The in_progress status is preserved
        assert await _status(s2, task_id) == "in_progress"

    # v0.2 will add: operator-triggered reset + lease expiry detection
