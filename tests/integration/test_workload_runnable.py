"""Step 8 prep — Workload / Task Runnable Semantics acceptance tests.

Per docs/specs/workload-runnable-semantics.md (ADR 0013):

  Acceptance criteria (mapped from ADR 0013):
    1.  A task with no dependencies is runnable
    2.  A task with all deps `completed` is runnable
    3.  A task with a dep `in_progress` (pending) is NOT runnable
    4.  A task with a dep `failed` is NOT runnable
    5.  Runnable tasks are returned in FIFO order
        (`created_at ASC, id ASC`)
    6.  A dependency chain becomes runnable only after the
        upstream dep completes
    7.  The orchestrator only picks runnable tasks — a `queued`
        task with unsatisfied deps is skipped
    8.  `next_runnable()` returns a candidate without claiming it
        — the kernel still owns the claim

These tests are SKIPPED until Step 8b lands the implementation:
  - `WorkloadService.next_runnable()` (extends the existing
    `WorkloadService`, no new module)
  - `WorkerOrchestrator.process_one()` calls `next_runnable()`
    instead of the raw `SELECT ... WHERE status='queued'` query

The skip guard uses ``hasattr(WorkloadService, 'next_runnable')``
because Step 8 ADDS A METHOD to the existing ``WorkloadService``
(rather than introducing a new module, which is what Steps 6 and 7
did). The hasattr check evaluates once at import time and decides
whether the Step 8b implementation is in place.

These tests verify the worker's interaction with Workload / kernel /
runnable-task layer — the chain test methodology from Step 5 applied
to scheduling semantics.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from personaos.domain.task import Task
from personaos.domain.worker import Persona, WorkerProfile
from personaos.execution.adapters.base import Result
from personaos.execution.context import WorkerExecutionContext
from personaos.execution.kernel import WorkerExecutionKernel
from personaos.registry.profile import ProfileRegistry
from personaos.workload.service import WorkloadService

# Step 8b implementation gate: ``WorkloadService.next_runnable()`` lands
# in Step 8b. The hasattr check is evaluated once at import time.
# Until Step 8b lands, this is False and all tests in this module
# are skipped. Once Step 8b lands, this becomes True and the skipif
# is a no-op.
_STEP_8B_AVAILABLE = hasattr(WorkloadService, "next_runnable")


# tests/integration/test_workload_runnable.py → repo root → src/personaos/db/migrations
REPO_ROOT = Path(__file__).parents[2]
ALEMBIC_INI = REPO_ROOT / "src" / "personaos" / "db" / "migrations" / "alembic.ini"
MIGRATIONS_DIR = REPO_ROOT / "src" / "personaos" / "db" / "migrations"

pytestmark = [
    pytest.mark.usefixtures("clean_tables"),
    pytest.mark.skipif(
        not _STEP_8B_AVAILABLE,
        reason=(
            "Step 8b implementation not landed yet: "
            "WorkloadService.next_runnable() (extends WorkloadService) "
            "+ WorkerOrchestrator.process_one() uses next_runnable()"
        ),
    ),
]


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
    """Records the context it receives; returns a configurable Result."""

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


# ---------- Helpers ----------


async def _setup_worker_v2_active(
    session, profiles_dir, *, worker_id: str = "w1"
) -> ProfileRegistry:
    """Register w1 with v1 + v2 profiles, activate v2."""
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
            persona=Persona(autonomy="low"),
        ),
    )
    await reg.activate_profile_version(worker_id, 2)
    return reg


async def _make_kernel(session, profiles_dir):
    """Build a kernel wired to a fresh ProfileRegistry."""
    adapter = CapturingAdapter(
        result_to_return=Result(status="completed", output={"ok": True})
    )
    reg = ProfileRegistry(session=session, profiles_dir=profiles_dir)
    kernel = WorkerExecutionKernel(
        session=session,
        adapter=adapter,
        resolve_context=reg.resolve_execution_context,
    )
    return kernel, adapter


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


# ---------- AC #1: task with no dependencies is runnable ----------


async def test_task_without_dependencies_is_runnable(
    profiles_dir, database_url, session_factory
):
    """A queued task with empty dependencies list is returned by next_runnable()."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker_v2_active(session, profiles_dir)
        wl = WorkloadService(session=session)
        task = await wl.enqueue(
            worker_id="w1",
            title="solo",
            objective="no deps",
            input={"x": 1},
        )
        # Confirm: dependencies are empty
        assert task.dependencies == []

        candidate = await wl.next_runnable()

        assert candidate is not None
        assert candidate.id == task.id
        # The candidate is not claimed — status remains 'queued'
        assert await _task_status(session, task.id) == "queued"


# ---------- AC #2: task with all deps completed is runnable ----------


async def test_task_with_all_dependencies_completed_is_runnable(
    profiles_dir, database_url, session_factory
):
    """A queued task whose deps are all 'completed' is runnable."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker_v2_active(session, profiles_dir)
        wl = WorkloadService(session=session)
        # Parent task
        parent = await wl.enqueue(
            worker_id="w1",
            title="parent",
            objective="parent task",
            input={"p": 1},
        )
        # Child task depending on parent
        child = await wl.enqueue(
            worker_id="w1",
            title="child",
            objective="depends on parent",
            input={"c": 1},
            dependencies=[parent.id],
        )

        # Manually mark parent as completed (we want to test the runnable
        # gate, not the full lifecycle)
        await session.execute(
            text("UPDATE tasks SET status = 'completed' WHERE id = :tid"),
            {"tid": parent.id},
        )
        await session.commit()

        candidate = await wl.next_runnable()

        # Child should be runnable since its only dep (parent) is completed
        assert candidate is not None
        assert candidate.id == child.id


# ---------- AC #3: task with pending dep is NOT runnable ----------


async def test_task_with_pending_dependency_is_not_runnable(
    profiles_dir, database_url, session_factory
):
    """A queued task whose dep is still 'in_progress' is NOT runnable."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker_v2_active(session, profiles_dir)
        wl = WorkloadService(session=session)
        # Parent task — leave it in 'in_progress' (NOT completed)
        parent = await wl.enqueue(
            worker_id="w1",
            title="parent",
            objective="parent task",
            input={"p": 1},
        )
        await session.execute(
            text("UPDATE tasks SET status = 'in_progress' WHERE id = :tid"),
            {"tid": parent.id},
        )
        await session.commit()

        # Child task depending on parent
        child = await wl.enqueue(
            worker_id="w1",
            title="child",
            objective="depends on in-progress parent",
            input={"c": 1},
            dependencies=[parent.id],
        )

        candidate = await wl.next_runnable()

        # Child should NOT be runnable; parent is in_progress, not completed
        # next_runnable may return None (no runnable work) or some other
        # task — but never the child.
        if candidate is not None:
            assert candidate.id != child.id, (
                "task with an in_progress dep should not be runnable"
            )


# ---------- AC #4: task with failed dep is NOT runnable ----------


async def test_task_with_failed_dependency_is_not_runnable(
    profiles_dir, database_url, session_factory
):
    """A queued task whose dep is 'failed' is NOT runnable."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker_v2_active(session, profiles_dir)
        wl = WorkloadService(session=session)
        # Parent task — mark as 'failed'
        parent = await wl.enqueue(
            worker_id="w1",
            title="parent",
            objective="parent task",
            input={"p": 1},
        )
        await session.execute(
            text("UPDATE tasks SET status = 'failed' WHERE id = :tid"),
            {"tid": parent.id},
        )
        await session.commit()

        # Child task depending on failed parent
        child = await wl.enqueue(
            worker_id="w1",
            title="child",
            objective="depends on failed parent",
            input={"c": 1},
            dependencies=[parent.id],
        )

        candidate = await wl.next_runnable()

        # Child should NOT be runnable — failed deps never recover
        if candidate is not None:
            assert candidate.id != child.id, (
                "task with a failed dep should not be runnable"
            )


# ---------- AC #5: runnable tasks are FIFO ordered ----------


async def test_runnable_tasks_are_fifo_ordered(
    profiles_dir, database_url, session_factory
):
    """next_runnable() returns the OLDEST runnable task first.

    Three tasks with no dependencies, enqueued in order T1, T2, T3.
    next_runnable() must return T1 first, then T2, then T3.
    Tie-breaker: id ASC (deterministic).
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker_v2_active(session, profiles_dir)
        wl = WorkloadService(session=session)
        t1 = await wl.enqueue(worker_id="w1", title="T1", objective="1", input={"i": 1})
        t2 = await wl.enqueue(worker_id="w1", title="T2", objective="2", input={"i": 2})
        t3 = await wl.enqueue(worker_id="w1", title="T3", objective="3", input={"i": 3})

        # FIFO: T1 first
        c1 = await wl.next_runnable()
        assert c1 is not None and c1.id == t1.id

        # Mark T1 complete, then next_runnable returns T2
        await session.execute(
            text("UPDATE tasks SET status = 'completed' WHERE id = :tid"),
            {"tid": t1.id},
        )
        await session.commit()
        c2 = await wl.next_runnable()
        assert c2 is not None and c2.id == t2.id

        # Mark T2 complete, then next_runnable returns T3
        await session.execute(
            text("UPDATE tasks SET status = 'completed' WHERE id = :tid"),
            {"tid": t2.id},
        )
        await session.commit()
        c3 = await wl.next_runnable()
        assert c3 is not None and c3.id == t3.id


# ---------- AC #6: dependency chain becomes runnable after completion ----------


async def test_dependency_chain_becomes_runnable_after_completion(
    profiles_dir, database_url, session_factory
):
    """A 3-task chain A → B → C. B is runnable only after A completes;
    C is runnable only after B completes."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker_v2_active(session, profiles_dir)
        wl = WorkloadService(session=session)
        a = await wl.enqueue(worker_id="w1", title="A", objective="A", input={})
        b = await wl.enqueue(
            worker_id="w1", title="B", objective="B", input={}, dependencies=[a.id]
        )
        c = await wl.enqueue(
            worker_id="w1", title="C", objective="C", input={}, dependencies=[b.id]
        )

        # Phase 1: A is queued, B/C depend on it. Only A is runnable.
        c1 = await wl.next_runnable()
        assert c1 is not None and c1.id == a.id
        assert c1.id != b.id and c1.id != c.id

        # Mark A completed → B becomes runnable
        await session.execute(
            text("UPDATE tasks SET status = 'completed' WHERE id = :tid"),
            {"tid": a.id},
        )
        await session.commit()
        c2 = await wl.next_runnable()
        assert c2 is not None and c2.id == b.id
        assert c2.id != c.id

        # Mark B completed → C becomes runnable
        await session.execute(
            text("UPDATE tasks SET status = 'completed' WHERE id = :tid"),
            {"tid": b.id},
        )
        await session.commit()
        c3 = await wl.next_runnable()
        assert c3 is not None and c3.id == c.id

        # Mark C completed → nothing left
        await session.execute(
            text("UPDATE tasks SET status = 'completed' WHERE id = :tid"),
            {"tid": c.id},
        )
        await session.commit()
        c4 = await wl.next_runnable()
        assert c4 is None


# ---------- AC #7: orchestrator processes only runnable tasks ----------


async def test_orchestrator_processes_only_runnable_tasks(
    profiles_dir, database_url, session_factory
):
    """An orchestrator running process_one() must skip queued tasks with
    unsatisfied deps and process only the runnable ones."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker_v2_active(session, profiles_dir)

        wl = WorkloadService(session=session)
        # Two queued tasks — one with deps (should NOT be picked), one without
        parent = await wl.enqueue(
            worker_id="w1", title="parent", objective="parent", input={}
        )
        await session.execute(
            text("UPDATE tasks SET status = 'in_progress' WHERE id = :tid"),
            {"tid": parent.id},
        )
        await session.commit()

        blocked_child = await wl.enqueue(
            worker_id="w1",
            title="blocked",
            objective="depends on parent (still in_progress)",
            input={},
            dependencies=[parent.id],
        )
        free_task = await wl.enqueue(
            worker_id="w1",
            title="free",
            objective="no deps",
            input={"x": 1},
        )

        # Run one orchestrator iteration
        from personaos.orchestrator.worker import WorkerOrchestrator  # type: ignore

        kernel, adapter = await _make_kernel(session, profiles_dir)
        orch = WorkerOrchestrator(session=session, kernel=kernel)

        processed = await orch.process_one()

        # The orchestrator picked the FREE task, not the blocked one
        assert processed is True
        assert adapter.captured_context is not None
        assert adapter.captured_context.task.id == free_task.id
        # The blocked task is still queued (not claimed, not processed)
        assert await _task_status(session, blocked_child.id) == "queued"
        assert await _task_status(session, free_task.id) == "completed"


# ---------- AC #8: next_runnable() does NOT claim ----------


async def test_runnable_selection_does_not_claim_task(
    profiles_dir, database_url, session_factory
):
    """next_runnable() returns a candidate without performing the atomic UPDATE.
    The task's status remains 'queued' after the call. The kernel still
    owns the claim (Phase 3 UPDATE WHERE status='queued')."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker_v2_active(session, profiles_dir)
        wl = WorkloadService(session=session)
        task = await wl.enqueue(
            worker_id="w1", title="solo", objective="no deps", input={}
        )

        # Snapshot task state BEFORE next_runnable
        before = (
            await session.execute(
                text(
                    "SELECT status, started_at, completed_at FROM tasks "
                    "WHERE id = :tid"
                ),
                {"tid": task.id},
            )
        ).first()

        candidate = await wl.next_runnable()

        # Snapshot task state AFTER next_runnable
        after = (
            await session.execute(
                text(
                    "SELECT status, started_at, completed_at FROM tasks "
                    "WHERE id = :tid"
                ),
                {"tid": task.id},
            )
        ).first()

        # next_runnable returned the candidate
        assert candidate is not None
        assert candidate.id == task.id

        # CRITICAL: state is UNCHANGED after next_runnable
        # — no claim happened
        assert before.status == after.status == "queued"
        assert before.started_at == after.started_at
        assert before.completed_at == after.completed_at

        # Now invoke the kernel — only HERE does the atomic claim happen
        from personaos.orchestrator.worker import WorkerOrchestrator  # type: ignore

        kernel, _ = await _make_kernel(session, profiles_dir)
        orch = WorkerOrchestrator(session=session, kernel=kernel)
        await orch.process_one()

        # After the kernel lifecycle: status is 'completed' (atomic claim happened)
        final = await _task_status(session, task.id)
        assert final == "completed"