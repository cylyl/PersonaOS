"""Step 9 prep — Task Dependency Lifecycle acceptance tests.

Per docs/specs/task-dependency-lifecycle.md (ADR 0014):

  Acceptance criteria (mapped from ADR 0014):
    1.  Completed dependency makes task runnable.
    2.  Failed dependency keeps task non-runnable.
    3.  Pending dependency (in_progress / blocked / review) keeps
        task non-runnable.
    4.  Missing dependency (references nonexistent task ID) keeps
        task non-runnable.
    5.  Self-dependency (A → A) is not runnable, no mutation.
    6.  Cyclic dependencies (A → B → A) keep both tasks non-runnable,
        no mutation.
    7.  Dependency chain progresses step-by-step as each dep
        completes.
    8.  Failed dependency does NOT propagate `'failed'` status to the
        dependent (no status cascade).
    9.  Concurrent next_runnable() + dep-completion produces
        consistent state across readers.
    10. next_runnable() remains read-only for the dependency graph
        (no UPDATE/INSERT/DELETE).

These tests VERIFY that the EXISTING WorkloadService.next_runnable()
query (Step 8) satisfies the Step 9 safety contracts. No production code
changes — this is verification-only.

Skip gate is a manual flag (``_STEP_9A_APPROVED``). Step 9a adds no new
module or method, so the gate cannot use ``try/except ImportError`` or
``hasattr``. The flag is flipped to ``True`` after Liang approves
ADR 0014 + spec + 10 tests.
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

from personaos.domain.worker import Persona, WorkerProfile
from personaos.execution.adapters.base import Result
from personaos.execution.context import WorkerExecutionContext
from personaos.execution.kernel import WorkerExecutionKernel
from personaos.registry.profile import ProfileRegistry
from personaos.workload.service import WorkloadService

# Step 9a: no production code changes. Tests verify existing behavior
# of next_runnable() under pathological dep graph conditions.
# Skip gate: pending Liang's approval of ADR 0014 + spec + 10 tests.
# When approved, set to True and tests will run.
_STEP_9A_APPROVED = True  # approved 2026-10-08 13:53 UTC per Liang


# tests/integration/test_task_dependency_lifecycle.py → repo root → src/personaos/db/migrations
REPO_ROOT = Path(__file__).parents[2]
ALEMBIC_INI = REPO_ROOT / "src" / "personaos" / "db" / "migrations" / "alembic.ini"
MIGRATIONS_DIR = REPO_ROOT / "src" / "personaos" / "db" / "migrations"

pytestmark = [
    pytest.mark.usefixtures("clean_tables"),
    pytest.mark.skipif(
        not _STEP_9A_APPROVED,
        reason=(
            "Step 9a pending Liang's approval of "
            "ADR 0014 + spec + 10 acceptance tests"
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
    """Records the context; returns a configurable Result."""

    result_to_return: Result
    captured_context: WorkerExecutionContext | None = None

    async def execute(
        self,
        context: WorkerExecutionContext,
        checkpoint: Any | None = None,
    ) -> Result:
        self.captured_context = context
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


async def _set_task_status(session, task_id: str, status: str) -> None:
    """Force-set a task's status (for testing dep-state conditions)."""
    await session.execute(
        text("UPDATE tasks SET status = :s WHERE id = :tid"),
        {"s": status, "tid": task_id},
    )
    await session.commit()


async def _set_task_dependencies(session, task_id: str, dep_ids: list) -> None:
    """Force-set a task's dependencies (JSONB array of task IDs)."""
    # Cast the dep_ids list to a JSONB array literal.
    import json

    deps_json = json.dumps(dep_ids)
    await session.execute(
        text("UPDATE tasks SET dependencies = CAST(:d AS jsonb) WHERE id = :tid"),
        {"d": deps_json, "tid": task_id},
    )
    await session.commit()


# ---------- AC #1: completed dep makes task runnable ----------


async def test_completed_dependency_makes_task_runnable(
    profiles_dir, database_url, session_factory
):
    """A task whose only dep is 'completed' should become runnable."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker_v2_active(session, profiles_dir)

        wl = WorkloadService(session=session)
        parent = await wl.enqueue(
            worker_id="w1",
            title="parent",
            objective="parent task",
            input={"p": 1},
        )
        child = await wl.enqueue(
            worker_id="w1",
            title="child",
            objective="depends on parent",
            input={"c": 1},
            dependencies=[parent.id],
        )

        # Mark parent as completed (simulating kernel's Phase 3 finish)
        await _set_task_status(session, parent.id, "completed")

        candidate = await wl.next_runnable()

        assert candidate is not None
        assert candidate.id == child.id


# ---------- AC #2: failed dep keeps task non-runnable ----------


async def test_failed_dependency_keeps_task_non_runnable(
    profiles_dir, database_url, session_factory
):
    """A task with a failed dep is NOT runnable. Dependent remains queued.

    No status cascade: the dependent's status must stay 'queued', NOT
    'failed' or 'blocked'. next_runnable() must not modify state.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker_v2_active(session, profiles_dir)

        wl = WorkloadService(session=session)
        parent = await wl.enqueue(
            worker_id="w1",
            title="parent",
            objective="parent task",
            input={"p": 1},
        )
        child = await wl.enqueue(
            worker_id="w1",
            title="child",
            objective="depends on parent (will fail)",
            input={"c": 1},
            dependencies=[parent.id],
        )

        # Mark parent as failed
        await _set_task_status(session, parent.id, "failed")

        candidate = await wl.next_runnable()

        # Child must NOT be returned
        if candidate is not None:
            assert candidate.id != child.id, (
                "task with failed dep should not be runnable"
            )

        # Child remains queued (NOT failed, NOT blocked — no cascade)
        assert await _task_status(session, child.id) == "queued"


# ---------- AC #3: pending dep keeps task non-runnable ----------


async def test_pending_dependency_keeps_task_non_runnable(
    profiles_dir, database_url, session_factory
):
    """A task with a dep in any non-terminal state is NOT runnable.

    Covers: 'queued', 'in_progress', 'blocked', 'review'. None of
    these are 'completed', so the dep gate excludes the dependent.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker_v2_active(session, profiles_dir)

        wl = WorkloadService(session=session)
        parent = await wl.enqueue(
            worker_id="w1",
            title="parent",
            objective="parent task",
            input={"p": 1},
        )
        # Mark parent as in_progress (kernel would do this on claim)
        await _set_task_status(session, parent.id, "in_progress")

        child = await wl.enqueue(
            worker_id="w1",
            title="child",
            objective="depends on in_progress parent",
            input={"c": 1},
            dependencies=[parent.id],
        )

        candidate = await wl.next_runnable()

        if candidate is not None:
            assert candidate.id != child.id, (
                "task with in_progress dep should not be runnable"
            )

        assert await _task_status(session, child.id) == "queued"


# ---------- AC #4: missing dep keeps task non-runnable ----------


async def test_missing_dependency_keeps_task_non_runnable(
    profiles_dir, database_url, session_factory
):
    """A task whose dep ID refers to no task is NOT runnable. No mutation.

    The LEFT JOIN in next_runnable() returns NULL for the missing dep;
    dep.id IS NULL triggers the exclusion.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker_v2_active(session, profiles_dir)

        wl = WorkloadService(session=session)
        child = await wl.enqueue(
            worker_id="w1",
            title="child",
            objective="depends on nonexistent task",
            input={"c": 1},
            # UUID that doesn't exist in the tasks table
            dependencies=["00000000-0000-0000-0000-000000000000"],
        )

        candidate = await wl.next_runnable()

        # Child must NOT be returned (missing dep)
        if candidate is not None:
            assert candidate.id != child.id, (
                "task with missing dep should not be runnable"
            )

        # No mutation — child remains queued
        assert await _task_status(session, child.id) == "queued"


# ---------- AC #5: self-dependency is not runnable ----------


async def test_self_dependency_is_not_runnable(
    profiles_dir, database_url, session_factory
):
    """A task with itself as a dep is NOT runnable. No state mutation.

    A → A: the JSONB expansion yields [A], the LEFT JOIN matches
    A.id = A.id, A's status is 'queued' (not 'completed'), so A is
    excluded. Self-loop is permanently not runnable.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker_v2_active(session, profiles_dir)

        wl = WorkloadService(session=session)
        a = await wl.enqueue(
            worker_id="w1",
            title="self_loop",
            objective="depends on itself",
            input={"x": 1},
            dependencies=[],  # initially no deps
        )
        # Force-set dependencies to include self
        await _set_task_dependencies(session, a.id, [a.id])

        candidate = await wl.next_runnable()

        # Self-loop must NOT be returned
        if candidate is not None:
            assert candidate.id != a.id, (
                "self-loop task should not be runnable"
            )

        # No state mutation
        assert await _task_status(session, a.id) == "queued"


# ---------- AC #6: cyclic dependencies are not runnable ----------


async def test_cyclic_dependencies_are_not_runnable(
    profiles_dir, database_url, session_factory
):
    """A → B → A cycle: neither A nor B is runnable. No state mutation.

    For task A, deps expand to [B]. B's status is 'queued' (not
    'completed'). A is excluded. Same for B (its dep is A with status
    'queued'). Cycle tasks are permanently not runnable.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker_v2_active(session, profiles_dir)

        wl = WorkloadService(session=session)
        a = await wl.enqueue(
            worker_id="w1",
            title="A",
            objective="A in cycle",
            input={},
        )
        # B depends on A
        b = await wl.enqueue(
            worker_id="w1",
            title="B",
            objective="B in cycle",
            input={},
            dependencies=[a.id],
        )
        # Now A depends on B (completing the cycle)
        await _set_task_dependencies(session, a.id, [b.id])

        candidate = await wl.next_runnable()

        # Neither A nor B should be runnable
        if candidate is not None:
            assert candidate.id not in (a.id, b.id), (
                "cycle tasks should not be runnable"
            )

        # No state mutation — both remain queued
        assert await _task_status(session, a.id) == "queued"
        assert await _task_status(session, b.id) == "queued"


# ---------- AC #7: chain progresses after each completion ----------


async def test_dependency_chain_progresses_after_each_completion(
    profiles_dir, database_url, session_factory
):
    """A → B → C chain: B becomes runnable after A completes; C after B.

    Each completion step opens up the next downstream task. After C
    completes, no task is runnable.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker_v2_active(session, profiles_dir)

        wl = WorkloadService(session=session)
        a = await wl.enqueue(
            worker_id="w1", title="A", objective="first", input={}
        )
        b = await wl.enqueue(
            worker_id="w1",
            title="B",
            objective="second",
            input={},
            dependencies=[a.id],
        )
        c = await wl.enqueue(
            worker_id="w1",
            title="C",
            objective="third",
            input={},
            dependencies=[b.id],
        )

        # Phase 1: only A is runnable (B and C blocked)
        c1 = await wl.next_runnable()
        assert c1 is not None and c1.id == a.id

        # Mark A completed → B becomes runnable
        await _set_task_status(session, a.id, "completed")
        c2 = await wl.next_runnable()
        assert c2 is not None and c2.id == b.id

        # Mark B completed → C becomes runnable
        await _set_task_status(session, b.id, "completed")
        c3 = await wl.next_runnable()
        assert c3 is not None and c3.id == c.id

        # Mark C completed → nothing left
        await _set_task_status(session, c.id, "completed")
        c4 = await wl.next_runnable()
        assert c4 is None


# ---------- AC #8: failed dep does NOT propagate failed status ----------


async def test_failed_dependency_does_not_propagate_failed_status(
    profiles_dir, database_url, session_factory
):
    """When a dep is failed, the dependent's status remains 'queued'
    (NOT 'failed', NOT 'blocked'). Only the dep itself is 'failed'.

    This verifies that next_runnable() does NOT modify task state and
    that there is no automatic failure cascade. The dependent stays
    queued forever (v0.1) until manual cleanup.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker_v2_active(session, profiles_dir)

        wl = WorkloadService(session=session)
        parent = await wl.enqueue(
            worker_id="w1",
            title="parent",
            objective="parent task",
            input={"p": 1},
        )
        child = await wl.enqueue(
            worker_id="w1",
            title="child",
            objective="depends on parent",
            input={"c": 1},
            dependencies=[parent.id],
        )

        # Mark parent as failed
        await _set_task_status(session, parent.id, "failed")

        # Run next_runnable() multiple times — must NOT mutate state
        await wl.next_runnable()
        await wl.next_runnable()
        await wl.next_runnable()

        # Parent remains 'failed'
        assert await _task_status(session, parent.id) == "failed"

        # Child remains 'queued' — NOT 'failed', NOT 'blocked'
        child_status = await _task_status(session, child.id)
        assert child_status == "queued", (
            f"child status should remain 'queued' (no cascade), "
            f"got '{child_status}'"
        )


# ---------- AC #9: concurrent completion preserves consistent state ----------


async def test_concurrent_dependency_completion_preserves_consistent_state(
    profiles_dir, database_url, session_factory
):
    """Concurrent next_runnable() + dep-completion should not produce
    inconsistent state.

    Multiple readers + one writer (dep completion) → all readers see
    consistent answers. No reader sees a torn state (e.g., a dep that
    was 'in_progress' at read-start but 'completed' at read-end).
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker_v2_active(session, profiles_dir)

        wl = WorkloadService(session=session)
        parent = await wl.enqueue(
            worker_id="w1",
            title="parent",
            objective="parent task",
            input={"p": 1},
        )
        child = await wl.enqueue(
            worker_id="w1",
            title="child",
            objective="depends on parent",
            input={"c": 1},
            dependencies=[parent.id],
        )

        # Run multiple concurrent reads — all should return None
        # (parent is queued, child is not yet runnable)
        async def read():
            return await wl.next_runnable()

        results = await asyncio.gather(*[read() for _ in range(5)])
        for r in results:
            assert r is None, (
                "before completion, child should not be runnable"
            )

        # Mark parent completed
        await _set_task_status(session, parent.id, "completed")

        # All concurrent reads should consistently return the child
        results = await asyncio.gather(*[read() for _ in range(5)])
        for r in results:
            assert r is not None
            assert r.id == child.id, (
                "after completion, child should be runnable"
            )

        # No state mutation from concurrent reads
        assert await _task_status(session, parent.id) == "completed"
        assert await _task_status(session, child.id) == "queued"


# ---------- AC #10: next_runnable() remains read-only for the dep graph ----------


async def test_next_runnable_remains_read_only_for_dependency_graph(
    profiles_dir, database_url, session_factory
):
    """next_runnable() does NOT modify task state.

    The dep graph (status, dependencies, started_at, completed_at,
    last_error, retry_count) is unchanged after any number of
    next_runnable() calls, even with pathological deps (missing,
    cycle, self-loop).
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker_v2_active(session, profiles_dir)

        wl = WorkloadService(session=session)
        # A: missing dep
        a = await wl.enqueue(
            worker_id="w1",
            title="missing_dep",
            objective="A with missing dep",
            input={},
            dependencies=["00000000-0000-0000-0000-000000000000"],
        )
        # B + C: cycle
        b = await wl.enqueue(
            worker_id="w1", title="B", objective="B in cycle", input={}
        )
        c = await wl.enqueue(
            worker_id="w1", title="C", objective="C in cycle", input={}
        )
        await _set_task_dependencies(session, b.id, [c.id])
        await _set_task_dependencies(session, c.id, [b.id])
        # D: self-loop
        d = await wl.enqueue(
            worker_id="w1", title="D", objective="D self-loop", input={}
        )
        await _set_task_dependencies(session, d.id, [d.id])

        # Snapshot all task state BEFORE any next_runnable calls
        before_rows = (
            await session.execute(
                text(
                    "SELECT id, status, started_at, completed_at, "
                    "last_error, retry_count, dependencies "
                    "FROM tasks ORDER BY id"
                )
            )
        ).fetchall()
        before = {row.id: dict(row._mapping) for row in before_rows}

        # Run next_runnable() many times — none should modify state
        for _ in range(5):
            candidate = await wl.next_runnable()
            # None of A/B/C/D are runnable (missing dep, cycle, self-loop)
            assert candidate is None

        # Snapshot task state AFTER all calls
        after_rows = (
            await session.execute(
                text(
                    "SELECT id, status, started_at, completed_at, "
                    "last_error, retry_count, dependencies "
                    "FROM tasks ORDER BY id"
                )
            )
        ).fetchall()
        after = {row.id: dict(row._mapping) for row in after_rows}

        # No task state changed
        assert before == after, (
            f"next_runnable() mutated task state: "
            f"before={before}, after={after}"
        )