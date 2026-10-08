"""Step 7 prep — Worker Orchestrator acceptance tests.

Per docs/specs/worker-orchestrator.md (ADR 0012):

  Acceptance criteria (mapped from ADR 0012):
    1.  Orchestrator processes a queued task
       (find → kernel.execute → completed)
    2.  Multiple tasks processed in sequence
    3.  Empty queue → no crash, returns False
    4.  stop() causes run() to exit cleanly
    5.  Two orchestrators on the same queued task → only ONE claims
       (concurrency boundary — the first proof of multi-orchestrator
       correctness in PersonaOS)
    6.  Orchestrator is adapter-agnostic (no OpenClaw imports)
    7.  Orchestrator uses the existing kernel (no parallel execution path)
    8.  No leases / failover / execution_node (deferred, static check)
    9.  Default poll_interval prevents tight CPU loops

These tests are SKIPPED until Step 7b lands the implementation:
  - personaos.orchestrator.worker.WorkerOrchestrator + OrchestratorMetrics

Once unskipped, they verify:
  - the polling-loop pattern (find + invoke + handle concurrency)
  - the first concurrency boundary in PersonaOS
  - that the orchestrator does NOT bypass the kernel
  - that the orchestrator does NOT introduce leases/failover
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from personaos.db.models import Task as TaskModel
from personaos.domain.worker import Persona, WorkerProfile
from personaos.execution.adapters.base import Result
from personaos.execution.context import WorkerExecutionContext
from personaos.execution.kernel import TaskStatusError, WorkerExecutionKernel
from personaos.registry.profile import ProfileRegistry
from personaos.workload.service import WorkloadService

# Step 7b imports — these do not exist yet. The whole module is skipped
# until they land. After Step 7b, the guard becomes a no-op and the
# tests run against the real implementation.
try:
    from personaos.orchestrator.metrics import OrchestratorMetrics
    from personaos.orchestrator.worker import WorkerOrchestrator

    _STEP_7B_AVAILABLE = True
except ImportError:
    _STEP_7B_AVAILABLE = False
    OrchestratorMetrics = None  # type: ignore[assignment]
    WorkerOrchestrator = None  # type: ignore[assignment]


# tests/integration/test_worker_orchestrator.py → repo root → src/personaos/db/migrations
REPO_ROOT = Path(__file__).parents[2]
ALEMBIC_INI = REPO_ROOT / "src" / "personaos" / "db" / "migrations" / "alembic.ini"
MIGRATIONS_DIR = REPO_ROOT / "src" / "personaos" / "db" / "migrations"

pytestmark = [
    pytest.mark.usefixtures("clean_tables"),
    pytest.mark.skipif(
        not _STEP_7B_AVAILABLE,
        reason=(
            "Step 7b implementation not landed yet: "
            "personaos.orchestrator.worker.WorkerOrchestrator "
            "+ personaos.orchestrator.metrics.OrchestratorMetrics"
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
    session, profiles_dir, *, worker_id: str = "w1", v2_autonomy: str = "low"
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
            persona=Persona(autonomy=v2_autonomy),
        ),
    )
    await reg.activate_profile_version(worker_id, 2)
    return reg


async def _enqueue_n(
    session, *, worker_id: str = "w1", n: int = 1
) -> list:
    """Enqueue n tasks. Returns list of Task domain objects."""
    wl = WorkloadService(session=session)
    tasks = []
    for i in range(n):
        task = await wl.enqueue(
            worker_id=worker_id,
            title=f"T{i}",
            objective=f"O{i}",
            input={"i": i},
        )
        tasks.append(task)
    return tasks


def _make_kernel(session, adapter, profiles_dir) -> WorkerExecutionKernel:
    """Build a kernel wired to a fresh ProfileRegistry on this session."""
    reg = ProfileRegistry(session=session, profiles_dir=profiles_dir)
    return WorkerExecutionKernel(
        session=session,
        adapter=adapter,
        resolve_context=reg.resolve_execution_context,
    )


def _make_orchestrator(session, kernel, **kwargs) -> WorkerOrchestrator:
    """Build a WorkerOrchestrator with sane defaults for tests."""
    return WorkerOrchestrator(session=session, kernel=kernel, **kwargs)


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


# ---------- AC #1: orchestrator processes a queued task ----------


async def test_orchestrator_processes_queued_task(
    profiles_dir, database_url, session_factory
):
    """Enqueue 1 task, call process_one() once, verify the task is completed."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker_v2_active(session, profiles_dir)
        tasks = await _enqueue_n(session, n=1)

        adapter = CapturingAdapter(
            result_to_return=Result(status="completed", output={"ok": True})
        )
        kernel = _make_kernel(session, adapter, profiles_dir)
        orch = _make_orchestrator(session, kernel)

        # process_one finds the queued task, invokes the kernel, returns True
        processed = await orch.process_one()

        assert processed is True
        assert orch.metrics.tasks_processed == 1
        assert orch.metrics.claim_conflicts == 0
        # The adapter received the task (proves kernel was called)
        assert adapter.captured_context is not None
        assert adapter.captured_context.task.id == tasks[0].id
        # The task is now 'completed' in the DB
        assert await _task_status(session, tasks[0].id) == "completed"


# ---------- AC #2: multiple tasks processed in sequence ----------


async def test_orchestrator_processes_multiple_tasks_in_sequence(
    profiles_dir, database_url, session_factory
):
    """Enqueue N tasks, call process_one() N times, verify all completed."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker_v2_active(session, profiles_dir)
        N = 3
        tasks = await _enqueue_n(session, n=N)

        adapter = CapturingAdapter(
            result_to_return=Result(status="completed", output={"ok": True})
        )
        kernel = _make_kernel(session, adapter, profiles_dir)
        orch = _make_orchestrator(session, kernel)

        # Process each task one at a time. Each process_one() returns True
        # and the next finds the next queued task.
        for expected_i in range(N):
            processed = await orch.process_one()
            assert processed is True, f"iteration {expected_i} returned False"

        assert orch.metrics.tasks_processed == N
        assert orch.metrics.claim_conflicts == 0
        # All tasks are completed
        for t in tasks:
            assert await _task_status(session, t.id) == "completed"


# ---------- AC #3: empty queue → no crash, returns False ----------


async def test_orchestrator_polls_when_queue_empty(
    profiles_dir, database_url, session_factory
):
    """No queued tasks: process_one returns False, no crash."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        # No tasks enqueued. Worker may or may not exist; doesn't matter.
        adapter = CapturingAdapter(
            result_to_return=Result(status="completed", output={})
        )
        kernel = _make_kernel(session, adapter, profiles_dir)
        orch = _make_orchestrator(session, kernel)

        processed = await orch.process_one()

        assert processed is False
        assert orch.metrics.tasks_processed == 0
        assert orch.metrics.claim_conflicts == 0
        # The adapter was NOT called (no work, no kernel invocation)
        assert adapter.captured_context is None


# ---------- AC #4: stop() causes run() to exit cleanly ----------


async def test_orchestrator_stops_cleanly(
    profiles_dir, database_url, session_factory
):
    """stop() during run() causes the run() loop to exit."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker_v2_active(session, profiles_dir)
        await _enqueue_n(session, n=1)

        adapter = CapturingAdapter(
            result_to_return=Result(status="completed", output={"ok": True})
        )
        kernel = _make_kernel(session, adapter, profiles_dir)
        # Short poll interval so the test finishes quickly
        orch = _make_orchestrator(session, kernel, poll_interval=0.05)

        assert orch.is_running is False

        run_task = asyncio.create_task(orch.run())
        # Give it a moment to start and process the queued task
        for _ in range(20):
            if orch.metrics.tasks_processed >= 1:
                break
            await asyncio.sleep(0.05)

        assert orch.is_running is True
        orch.stop()

        # run() must exit within a reasonable timeout
        await asyncio.wait_for(run_task, timeout=2.0)

        assert orch.is_running is False
        assert orch.metrics.tasks_processed == 1


# ---------- AC #5: two orchestrators → only ONE claims per task ----------


async def test_two_orchestrators_only_one_claims_same_task(
    profiles_dir, database_url, session_factory
):
    """Concurrency boundary: 2 orchestrators on the same queued task →
    only ONE successfully processes it.

    The other either:
      (a) SELECTs the task before the first orchestrator's kernel commits,
          then catches TaskStatusError from the kernel's atomic claim, OR
      (b) SELECTs after the first orchestrator's commit and finds nothing.

    In either case: total tasks_processed == 1, total claim_conflicts may
    be 0 or 1 (depends on timing).
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    # Setup + enqueue (separate session to keep things clean)
    async with session_factory() as session:
        await _setup_worker_v2_active(session, profiles_dir)
        tasks = await _enqueue_n(session, n=1)
        source_task_id = tasks[0].id

    # Two SEPARATE sessions for the two orchestrators. Sharing a session
    # across asyncio tasks is unsafe — each orchestrator needs its own.
    async with session_factory() as session1, session_factory() as session2:
        adapter1 = CapturingAdapter(
            result_to_return=Result(status="completed", output={"orch": 1})
        )
        adapter2 = CapturingAdapter(
            result_to_return=Result(status="completed", output={"orch": 2})
        )
        kernel1 = _make_kernel(session1, adapter1, profiles_dir)
        kernel2 = _make_kernel(session2, adapter2, profiles_dir)
        orch1 = _make_orchestrator(session1, kernel1)
        orch2 = _make_orchestrator(session2, kernel2)

        # Run both process_one() concurrently
        results = await asyncio.gather(
            orch1.process_one(),
            orch2.process_one(),
        )

        # Exactly ONE returned True (the other either got False from an
        # empty SELECT or got False from a caught TaskStatusError).
        assert sum(results) == 1, (
            f"expected exactly 1 successful process_one, got {results}"
        )

        # Total tasks_processed across both orchestrators == 1
        total_processed = (
            orch1.metrics.tasks_processed + orch2.metrics.tasks_processed
        )
        assert total_processed == 1, (
            f"expected exactly 1 task processed, got {total_processed}"
        )

        # The task is completed (the one orchestrator that succeeded)
        status = (
            await session1.execute(
                text("SELECT status FROM tasks WHERE id = :tid"),
                {"tid": source_task_id},
            )
        ).scalar()
        assert status == "completed"

        # The other orchestrator may have caught a TaskStatusError.
        # This is timing-dependent, so we don't assert on its exact value —
        # but the sum across both must be ≤ 1 (one conflict max).
        total_conflicts = (
            orch1.metrics.claim_conflicts + orch2.metrics.claim_conflicts
        )
        assert total_conflicts <= 1, (
            f"expected ≤ 1 claim conflict, got {total_conflicts}"
        )


# ---------- AC #6: orchestrator is adapter-agnostic (static check) ----------


def test_orchestrator_does_not_import_openclaw():
    """WorkerOrchestrator source must not import or reference OpenClaw.

    The orchestrator is a polling loop — it doesn't know what the Adapter
    does. Only the kernel touches OpenClaw.
    """
    p = (
        Path(__file__).parents[2]
        / "src"
        / "personaos"
        / "orchestrator"
        / "worker.py"
    )
    if not p.exists():
        pytest.skip("WorkerOrchestrator source not yet created (Step 7b)")
    src = p.read_text(encoding="utf-8")
    for forbidden in ("OpenClawAdapter", "openclaw", "OPENCLAW_API_URL"):
        if (
            f"import {forbidden}" in src
            or f"from personaos.execution.adapters.{forbidden.lower().replace('_adapter', '')}" in src
            or f"{forbidden}" in src.split('"""')[0]  # skip module docstring
        ):
            raise AssertionError(
                f"WorkerOrchestrator references {forbidden!r}; must be adapter-agnostic"
            )


# ---------- AC #7: orchestrator uses the existing kernel ----------


def test_orchestrator_uses_existing_kernel():
    """WorkerOrchestrator imports and uses WorkerExecutionKernel — no parallel path.

    Static check: the source must reference WorkerExecutionKernel. If it
    defined its own `execute_task` or similar that bypassed the kernel, the
    abstraction would be lost.
    """
    p = (
        Path(__file__).parents[2]
        / "src"
        / "personaos"
        / "orchestrator"
        / "worker.py"
    )
    if not p.exists():
        pytest.skip("WorkerOrchestrator source not yet created (Step 7b)")
    src = p.read_text(encoding="utf-8")
    if "WorkerExecutionKernel" not in src:
        raise AssertionError(
            "WorkerOrchestrator does not reference WorkerExecutionKernel; "
            "the abstraction is lost"
        )


# ---------- AC #8: no leases / failover / execution_node (static check) ----------


def test_orchestrator_no_leases_no_failover():
    """WorkerOrchestrator source must not contain v0.2 fields.

    Parses the source with ast and checks the module body for forbidden
    identifiers. Defers leases, failover, execution_node to v0.2.
    """
    import ast

    p = (
        Path(__file__).parents[2]
        / "src"
        / "personaos"
        / "orchestrator"
        / "worker.py"
    )
    if not p.exists():
        pytest.skip("WorkerOrchestrator source not yet created (Step 7b)")
    src = p.read_text(encoding="utf-8")
    tree = ast.parse(src)

    # Collect all identifier names that appear at module level (function
    # defs, class defs, top-level assignments, imported names).
    identifiers: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            identifiers.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                identifiers.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                identifiers.add(alias.asname or alias.name)

    forbidden = {
        "execution_node",
        "lease",
        "LeaseManager",
        "failover",
        "FailoverController",
        "rebalance",
        "Rebalancer",
    }
    leaked = identifiers & forbidden
    assert not leaked, (
        f"v0.2 identifiers leaked into orchestrator: {leaked}. "
        f"Leases / failover / execution_node are deferred to v0.2."
    )


# ---------- AC #9: default poll_interval prevents tight CPU loops ----------


def test_orchestrator_default_poll_interval():
    """Default poll_interval must be ≥ 50ms (no tight CPU loop when idle)."""
    if WorkerOrchestrator is None:
        pytest.skip("WorkerOrchestrator not yet imported (Step 7b)")
    sig = inspect.signature(WorkerOrchestrator.__init__)
    poll_param = sig.parameters.get("poll_interval")
    assert poll_param is not None, (
        "WorkerOrchestrator.__init__ has no poll_interval parameter"
    )
    assert poll_param.default is not inspect.Parameter.empty, (
        "poll_interval has no default"
    )
    assert poll_param.default >= 0.05, (
        f"poll_interval default {poll_param.default}s is too aggressive; "
        f"must be >= 0.05s to prevent CPU spin"
    )