"""Step 5 — Worker Execution Kernel acceptance tests.

Per docs/specs/worker-execution-kernel.md (ADR 0010).

  Acceptance criteria (mapped from ADR 0010):
    1.  Queued task executes with its snapshot
    2.  RUNNING → COMPLETED
    3.  RUNNING → FAILED
    4.  Execution context passed unchanged to adapter
    5.  Adapter failure becomes task failure
    6.  Successful result persisted
    7.  Failed result/error persisted
    8.  Current worker profile irrelevant after context resolution
    9.  No retry/lease behavior introduced
    10. Existing 34 tests remain green (verified by full suite, not a single test)

These tests verify the kernel orchestrates the lifecycle around the
now-stable WorkerExecutionContext from Step 4, and that the kernel itself
never touches worker.current_profile_version.

Note on DB state verification: tests use raw SQL via `text()` rather
than `session.get(TaskModel, ...)`. The session_factory is configured
with `expire_on_commit=False`, which means the in-memory ORM task stays
at its pre-execute values after the kernel's commits. Raw SQL bypasses
the identity map and reads fresh data from the DB.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from personaos.db.models import Task as TaskModel
from personaos.domain.task import Task
from personaos.domain.worker import Persona, WorkerProfile
from personaos.execution.context import WorkerExecutionContext
from personaos.registry.profile import ProfileRegistry
from personaos.workload.service import WorkloadService

# Step 5b imports — these must be available now that 5b has landed.
from personaos.execution.adapters.base import Result
from personaos.execution.kernel import (
    TaskStatusError,
    WorkerExecutionKernel,
)


# tests/integration/test_worker_execution_kernel.py → repo root → src/personaos/db/migrations
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


# ---------- Test doubles ----------


@dataclass
class CapturingAdapter:
    """Records the context + checkpoint it receives; returns a configurable Result.

    Used to verify the snapshot semantics: the adapter sees task.profile_version,
    not worker.current_profile_version.
    """

    result_to_return: Result
    captured_context: WorkerExecutionContext | None = None
    captured_checkpoint: Any | None = "_UNSET"

    async def execute(
        self,
        context: WorkerExecutionContext,
        checkpoint: Any | None = None,
    ) -> Result:
        self.captured_context = context
        self.captured_checkpoint = checkpoint
        return self.result_to_return


class FailingAdapter:
    """Always raises RuntimeError. Used to test kernel's exception handling."""

    async def execute(
        self,
        context: WorkerExecutionContext,
        checkpoint: Any | None = None,
    ) -> Result:
        raise RuntimeError("adapter boom")


# ---------- Helpers ----------


async def _setup_worker_v1_v2_active(
    session, profiles_dir, *, worker_id: str = "w1", v2_autonomy: str = "low"
) -> ProfileRegistry:
    """Register w1 with v1 + v2 profiles, activate v2.

    worker.current_profile_version == 2 after this returns.
    """
    reg = ProfileRegistry(session=session, profiles_dir=profiles_dir)
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
    await reg.create_profile_version(
        worker_id,
        WorkerProfile(
            version=2,
            role="devops",
            runtime="openclaw",
            max_concurrent_tasks=2,
            persona=Persona(autonomy=v2_autonomy),
        ),
    )
    await reg.activate_profile_version(worker_id, 2)
    return reg


async def _enqueue_task(
    session, *, worker_id: str = "w1", title: str = "A", objective: str = "A"
) -> Task:
    """Enqueue a task with minimal payload."""
    wl = WorkloadService(session=session)
    return await wl.enqueue(
        worker_id=worker_id, title=title, objective=objective, input={}
    )


def _make_kernel(
    session, adapter, registry: ProfileRegistry
) -> WorkerExecutionKernel:
    """Build a kernel wired to the registry's resolve_execution_context."""
    return WorkerExecutionKernel(
        session=session,
        adapter=adapter,
        resolve_context=registry.resolve_execution_context,
    )


async def _task_status(session, task_id: str) -> str:
    """Read the current task status from the DB (raw SQL)."""
    row = (
        await session.execute(
            text("SELECT status FROM tasks WHERE id = :tid"),
            {"tid": task_id},
        )
    ).first()
    assert row is not None, f"task {task_id} disappeared"
    return row.status


async def _task_full_row(session, task_id: str):
    """Read all task fields from the DB (raw SQL)."""
    return (
        await session.execute(
            text(
                "SELECT status, completed_at, result, last_error, "
                "retry_count, checkpoint_ref FROM tasks WHERE id = :tid"
            ),
            {"tid": task_id},
        )
    ).first()


# ---------- AC #1 + #4 + #8: queued task executes with its snapshot ----------


async def test_queued_task_executes_with_its_snapshot(
    profiles_dir, database_url, session_factory
):
    """Task enqueued under v2 executes with v2 profile, even after worker → v3.

    Proves:
      - AC #1: queued task executes (and uses its snapshot)
      - AC #4: execution context passed unchanged to adapter
      - AC #8: current worker profile is irrelevant after resolution
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        reg = await _setup_worker_v1_v2_active(session, profiles_dir)
        task = await _enqueue_task(session)
        assert task.profile_version == 2

        # Activate v3 — worker.current is now 3 (different autonomy from v2)
        await reg.create_profile_version(
            "w1",
            WorkerProfile(
                version=3,
                role="devops",
                runtime="openclaw",
                max_concurrent_tasks=3,
                persona=Persona(autonomy="high"),  # distinct from v2's "low"
            ),
        )
        await reg.activate_profile_version("w1", 3)

        # Execute
        adapter = CapturingAdapter(
            result_to_return=Result(status="completed", output={"x": 1})
        )
        kernel = _make_kernel(session, adapter, reg)
        result = await kernel.execute(task)

        assert result.status == "completed"
        # The adapter received the v2 profile (the SNAPSHOT), NOT v3 (the current)
        assert adapter.captured_context is not None
        assert adapter.captured_context.profile.version == 2
        assert adapter.captured_context.profile.persona.autonomy == "low"
        # Sanity: worker IS on v3 (so the assertion above is meaningful)
        assert adapter.captured_context.worker.current_profile_version == 3


# ---------- AC #2 + #6: RUNNING → COMPLETED with result persistence ----------


async def test_running_to_completed_persists_result(
    profiles_dir, database_url, session_factory
):
    """Successful execution transitions to 'completed' and persists result.output."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        reg = await _setup_worker_v1_v2_active(session, profiles_dir)
        task = await _enqueue_task(session)
        adapter = CapturingAdapter(
            result_to_return=Result(status="completed", output={"y": 2})
        )
        kernel = _make_kernel(session, adapter, reg)
        result = await kernel.execute(task)

        assert result.status == "completed"
        # DB state (raw SQL — identity map is stale due to expire_on_commit=False)
        row = await _task_full_row(session, task.id)
        assert row.status == "completed"
        assert row.completed_at is not None
        assert row.result == {"y": 2}
        assert row.last_error is None


# ---------- AC #3 + #7: RUNNING → FAILED with error persistence ----------


async def test_running_to_failed_persists_error(
    profiles_dir, database_url, session_factory
):
    """Failed execution transitions to 'failed' and persists the error."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        reg = await _setup_worker_v1_v2_active(session, profiles_dir)
        task = await _enqueue_task(session)
        adapter = CapturingAdapter(
            result_to_return=Result(status="failed", error="test failure")
        )
        kernel = _make_kernel(session, adapter, reg)
        result = await kernel.execute(task)

        assert result.status == "failed"
        # DB state
        row = await _task_full_row(session, task.id)
        assert row.status == "failed"
        assert row.completed_at is not None
        assert row.last_error == "test failure"
        assert row.result == {"error": "test failure"}


# ---------- AC #5: adapter exception → task failure (kernel doesn't crash) ----------


async def test_adapter_exception_becomes_task_failure(
    profiles_dir, database_url, session_factory
):
    """An adapter that raises converts to a failed task (kernel survives)."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        reg = await _setup_worker_v1_v2_active(session, profiles_dir)
        task = await _enqueue_task(session)
        adapter = FailingAdapter()
        kernel = _make_kernel(session, adapter, reg)

        # Kernel must NOT propagate the adapter's RuntimeError
        result = await kernel.execute(task)

        assert result.status == "failed"
        assert "boom" in (result.error or "")
        # DB state
        row = await _task_full_row(session, task.id)
        assert row.status == "failed"
        assert row.last_error is not None
        assert "boom" in row.last_error


# ---------- AC #9: no retry/lease behavior on failure ----------


async def test_no_retry_on_failure(profiles_dir, database_url, session_factory):
    """Failed task does NOT auto-retry; retry_count stays 0; max_retries ignored."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        reg = await _setup_worker_v1_v2_active(session, profiles_dir)
        task = await _enqueue_task(session)
        adapter = CapturingAdapter(
            result_to_return=Result(status="failed", error="boom")
        )
        kernel = _make_kernel(session, adapter, reg)
        await kernel.execute(task)

        row = await _task_full_row(session, task.id)
        assert row.status == "failed"
        assert row.retry_count == 0  # No retry logic in Step 5
        # task stays 'failed' — no automatic escalation to queued/in_progress


# ---------- State guard: kernel rejects non-queued tasks ----------


async def test_kernel_rejects_non_queued_task(
    profiles_dir, database_url, session_factory
):
    """Kernel raises TaskStatusError if task is not in 'queued' state.

    Uses the atomic check-and-transition in Phase 3 — even if the in-memory
    Task dataclass still says 'queued' (stale), the WHERE clause in the
    UPDATE catches it.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        reg = await _setup_worker_v1_v2_active(session, profiles_dir)
        task = await _enqueue_task(session)

        # First execution transitions to completed
        adapter = CapturingAdapter(
            result_to_return=Result(status="completed", output={"a": 1})
        )
        kernel = _make_kernel(session, adapter, reg)
        await kernel.execute(task)

        # Verify DB state — task is now 'completed'
        status = await _task_status(session, task.id)
        assert status == "completed"

        # Second execution should raise TaskStatusError (task is now 'completed')
        with pytest.raises(TaskStatusError):
            await kernel.execute(task)


# ---------- Events: kernel emits task.claimed/completed/failed ----------


async def test_task_events_emitted_for_completed(
    profiles_dir, database_url, session_factory
):
    """Successful execution emits task.claimed + task.completed."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        reg = await _setup_worker_v1_v2_active(session, profiles_dir)
        task = await _enqueue_task(session)
        adapter = CapturingAdapter(
            result_to_return=Result(status="completed", output={"z": 3})
        )
        kernel = _make_kernel(session, adapter, reg)
        await kernel.execute(task)

        events = (
            await session.execute(
                text(
                    "SELECT type FROM task_events WHERE task_id = :tid "
                    "ORDER BY created_at"
                ),
                {"tid": task.id},
            )
        ).scalars().all()
        assert "task.claimed" in events
        assert "task.completed" in events
        assert "task.failed" not in events


async def test_task_events_emitted_for_failed(
    profiles_dir, database_url, session_factory
):
    """Failed execution emits task.claimed + task.failed."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        reg = await _setup_worker_v1_v2_active(session, profiles_dir)
        task = await _enqueue_task(session)
        adapter = CapturingAdapter(
            result_to_return=Result(status="failed", error="nope")
        )
        kernel = _make_kernel(session, adapter, reg)
        await kernel.execute(task)

        events = (
            await session.execute(
                text(
                    "SELECT type FROM task_events WHERE task_id = :tid "
                    "ORDER BY created_at"
                ),
                {"tid": task.id},
            )
        ).scalars().all()
        assert "task.claimed" in events
        assert "task.failed" in events
        assert "task.completed" not in events


# ---------- Checkpoint write: result.new_checkpoint is persisted ----------


async def test_checkpoint_written_if_result_has_new_checkpoint(
    profiles_dir, database_url, session_factory
):
    """If adapter returns Result(new_checkpoint=state), kernel writes + links it."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        reg = await _setup_worker_v1_v2_active(session, profiles_dir)
        task = await _enqueue_task(session)
        adapter = CapturingAdapter(
            result_to_return=Result(
                status="completed",
                output={"final": True},
                new_checkpoint={"step": 5, "state": "halfway"},
            )
        )
        kernel = _make_kernel(session, adapter, reg)
        await kernel.execute(task)

        # task.checkpoint_ref was updated (raw SQL — identity map is stale)
        cp_ref = (
            await session.execute(
                text("SELECT checkpoint_ref FROM tasks WHERE id = :tid"),
                {"tid": task.id},
            )
        ).scalar()
        assert cp_ref is not None

        # A new checkpoint row exists with the snapshotted state
        cp_row = (
            await session.execute(
                text(
                    "SELECT task_id, state FROM task_checkpoints WHERE ref = :ref"
                ),
                {"ref": cp_ref},
            )
        ).first()
        assert cp_row is not None
        assert cp_row.task_id == task.id
        assert cp_row.state == {"step": 5, "state": "halfway"}

        # A task.checkpointed event was emitted
        events = (
            await session.execute(
                text("SELECT type FROM task_events WHERE task_id = :tid"),
                {"tid": task.id},
            )
        ).scalars().all()
        assert "task.checkpointed" in events


# ---------- Checkpoint load: existing checkpoint is passed to adapter ----------


async def test_checkpoint_loaded_if_task_has_checkpoint_ref(
    profiles_dir, database_url, session_factory
):
    """If task.checkpoint_ref is set, the state dict is loaded and passed to adapter."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        reg = await _setup_worker_v1_v2_active(session, profiles_dir)
        task = await _enqueue_task(session)

        # Set up a checkpoint row linked to the task
        await session.execute(
            text(
                "INSERT INTO task_checkpoints (ref, task_id, state) "
                "VALUES ('cp-resume', :tid, '{\"step\": 3}'::jsonb)"
            ),
            {"tid": task.id},
        )
        await session.execute(
            text(
                "UPDATE tasks SET checkpoint_ref = 'cp-resume' WHERE id = :tid"
            ),
            {"tid": task.id},
        )
        await session.commit()

        # Refresh the in-memory task to pick up the new checkpoint_ref.
        # Without this, expire_on_commit=False would leave the cached object
        # with stale checkpoint_ref=None.
        task_row = await session.get(TaskModel, task.id)
        await session.refresh(task_row)
        task = Task.from_orm(task_row)
        assert task.checkpoint_ref == "cp-resume"  # sanity

        adapter = CapturingAdapter(
            result_to_return=Result(status="completed", output={"resumed": True})
        )
        kernel = _make_kernel(session, adapter, reg)
        await kernel.execute(task)

        # The adapter received the checkpoint state
        assert adapter.captured_checkpoint == {"step": 3}
