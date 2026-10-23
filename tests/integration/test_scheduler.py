"""Step 16 — Worker Skill & Capability Matching Scheduler integration tests.

Per ADR 0019 & docs/specs/worker-skill-capability-scheduler.md:
Validates all 15 acceptance scenarios:
  1.  test_unassigned_task_matched_to_qualified_worker
  2.  test_explicit_affinity_matched_to_assigned_worker
  3.  test_explicit_affinity_blocks_other_workers
  4.  test_required_skills_subset_matching
  5.  test_empty_required_skills_matches_any_worker
  6.  test_required_capabilities_subset_matching
  7.  test_empty_required_capabilities_matches_any_worker
  8.  test_node_affinity_filtering
  9.  test_priority_overrides_fifo_creation_order
  10. test_equal_priority_preserves_strict_fifo
  11. test_incomplete_dependency_blocks_selection
  12. test_completed_dependency_unblocks_selection
  13. test_head_of_line_blocking_prevented
  14. test_concurrent_runners_serialized_by_kernel_claim
  15. test_candidate_selection_is_read_only
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

from personaos.db.models import WorkerSkill
from personaos.domain.worker import Persona, WorkerProfile
from personaos.execution.adapters.base import Result
from personaos.execution.context import WorkerExecutionContext
from personaos.execution.kernel import TaskStatusError, WorkerExecutionKernel
from personaos.orchestrator.worker import WorkerOrchestrator
from personaos.registry.profile import ProfileRegistry
from personaos.workload.scheduler import WorkloadScheduler
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


@dataclass
class CapturingAdapter:
    """Mock adapter returning configurable Result."""

    result_to_return: Result
    captured_context: WorkerExecutionContext | None = None

    async def execute(
        self,
        context: WorkerExecutionContext,
        checkpoint: Any | None = None,
    ) -> Result:
        self.captured_context = context
        return self.result_to_return


async def _setup_worker(
    session: Any,
    profiles_dir: Path,
    worker_id: str,
    *,
    skills: list[str] | None = None,
    capabilities: list[str] | None = None,
    current_version: int = 1,
) -> ProfileRegistry:
    """Register worker and optionally populate skills and capabilities."""
    reg = ProfileRegistry(session=session, profiles_dir=profiles_dir)
    profile = WorkerProfile(
        version=1,
        role="engineer",
        runtime="openclaw",
        skills=list(skills or []),
        permissions=[],
        persona=Persona(autonomy="medium"),
    )
    await reg.register_worker(worker_id, profile)

    if current_version > 1:
        v2 = WorkerProfile(
            version=current_version,
            role="engineer",
            runtime="openclaw",
            skills=list(skills or []),
            permissions=[],
            persona=Persona(autonomy="low"),
        )
        await reg.create_profile_version(worker_id, v2)
        await reg.activate_profile_version(worker_id, current_version)

    # If extra capabilities provided, update workers.capabilities
    if capabilities is not None:
        await session.execute(
            text("UPDATE workers SET capabilities = CAST(:caps AS jsonb) WHERE id = :wid"),
            {"caps": str(capabilities).replace("'", '"'), "wid": worker_id},
        )
        await session.commit()

    return reg


# -----------------------------------------------------------------------------
# Acceptance Scenarios
# -----------------------------------------------------------------------------


async def test_unassigned_task_matched_to_qualified_worker(
    database_url: str, session_factory: Any, profiles_dir: Path
) -> None:
    """Scenario 1: Unassigned task matching worker skills is selected."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker(session, profiles_dir, "w-python", skills=["python"])
        wl = WorkloadService(session)
        task = await wl.enqueue(
            worker_id=None,
            title="Unassigned Python Task",
            objective="Do Python stuff",
            input={},
            required_skills=["python"],
        )
        assert task.assigned_worker is None

        scheduler = WorkloadScheduler(session)
        candidate = await scheduler.find_candidate(worker_id="w-python")
        assert candidate is not None
        assert candidate.id == task.id


async def test_explicit_affinity_matched_to_assigned_worker(
    database_url: str, session_factory: Any, profiles_dir: Path
) -> None:
    """Scenario 2: Pre-assigned task is selected by the assigned worker."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker(session, profiles_dir, "w-assigned", skills=["bash"])
        wl = WorkloadService(session)
        task = await wl.enqueue(
            worker_id="w-assigned",
            title="Assigned Task",
            objective="Run on assigned worker",
            input={},
        )
        assert task.assigned_worker == "w-assigned"

        scheduler = WorkloadScheduler(session)
        candidate = await scheduler.find_candidate(worker_id="w-assigned")
        assert candidate is not None
        assert candidate.id == task.id


async def test_explicit_affinity_blocks_other_workers(
    database_url: str, session_factory: Any, profiles_dir: Path
) -> None:
    """Scenario 3: Pre-assigned task is invisible to other workers, and direct claim is fenced."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        reg_a = await _setup_worker(session, profiles_dir, "w-alpha")
        await _setup_worker(session, profiles_dir, "w-beta")
        wl = WorkloadService(session)
        task = await wl.enqueue(
            worker_id="w-alpha",
            title="Alpha-only Task",
            objective="Secret work",
            input={},
        )

        scheduler = WorkloadScheduler(session)
        # Beta cannot find Alpha's pre-assigned task
        candidate = await scheduler.find_candidate(worker_id="w-beta")
        assert candidate is None

        # Direct kernel claim by Beta is fenced and rejected
        adapter = CapturingAdapter(Result(status="completed", output={"done": True}))
        kernel_beta = WorkerExecutionKernel(
            session=session,
            adapter=adapter,
            resolve_context=reg_a.resolve_execution_context,
            worker_id="w-beta",
        )
        with pytest.raises(TaskStatusError):
            await kernel_beta.execute(task)

        # Confirm task in DB remains untouched
        row = (
            await session.execute(
                text("SELECT status, assigned_worker FROM tasks WHERE id = :id"),
                {"id": task.id},
            )
        ).first()
        assert row is not None
        assert row.status == "queued"
        assert row.assigned_worker == "w-alpha"


async def test_required_skills_subset_matching(
    database_url: str, session_factory: Any, profiles_dir: Path
) -> None:
    """Scenario 4: Worker possessing required skills matches; missing skill skips."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker(session, profiles_dir, "w-partial", skills=["python"])
        await _setup_worker(session, profiles_dir, "w-full", skills=["python", "docker", "k8s"])
        wl = WorkloadService(session)

        # Task requires both python and docker
        task = await wl.enqueue(
            worker_id=None,
            title="Multi-skill Task",
            objective="Deploy app",
            input={},
            required_skills=["python", "docker"],
        )

        scheduler = WorkloadScheduler(session)
        # w-partial has python, but lacks docker -> returns None
        candidate_partial = await scheduler.find_candidate(worker_id="w-partial")
        assert candidate_partial is None

        # w-full has both python and docker -> matches task
        candidate_full = await scheduler.find_candidate(worker_id="w-full")
        assert candidate_full is not None
        assert candidate_full.id == task.id

        # Also test skills via relational worker_skills join table
        session.add(WorkerSkill(worker_id="w-partial", skill="docker"))
        await session.commit()

        # Now w-partial possesses docker via worker_skills join table -> matches!
        candidate_now_qualified = await scheduler.find_candidate(worker_id="w-partial")
        assert candidate_now_qualified is not None
        assert candidate_now_qualified.id == task.id


async def test_empty_required_skills_matches_any_worker(
    database_url: str, session_factory: Any, profiles_dir: Path
) -> None:
    """Scenario 5: Task with required_skills = [] matches any worker."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker(session, profiles_dir, "w-noskills", skills=[])
        wl = WorkloadService(session)
        task = await wl.enqueue(
            worker_id=None,
            title="General Task",
            objective="Simple chore",
            input={},
            required_skills=[],
        )

        scheduler = WorkloadScheduler(session)
        candidate = await scheduler.find_candidate(worker_id="w-noskills")
        assert candidate is not None
        assert candidate.id == task.id


async def test_required_capabilities_subset_matching(
    database_url: str, session_factory: Any, profiles_dir: Path
) -> None:
    """Scenario 6: Worker possessing required capabilities matches; missing capability skips."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker(session, profiles_dir, "w-cap1", capabilities=["terminal"])
        await _setup_worker(
            session, profiles_dir, "w-cap2", capabilities=["terminal", "git", "aws"]
        )
        wl = WorkloadService(session)

        task = await wl.enqueue(
            worker_id=None,
            title="Cloud Task",
            objective="Git push and aws deploy",
            input={},
            required_capabilities=["terminal", "git"],
        )

        scheduler = WorkloadScheduler(session)
        assert await scheduler.find_candidate(worker_id="w-cap1") is None

        candidate = await scheduler.find_candidate(worker_id="w-cap2")
        assert candidate is not None
        assert candidate.id == task.id


async def test_empty_required_capabilities_matches_any_worker(
    database_url: str, session_factory: Any, profiles_dir: Path
) -> None:
    """Scenario 7: Task with required_capabilities = [] matches any worker."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker(session, profiles_dir, "w-base", capabilities=[])
        wl = WorkloadService(session)
        task = await wl.enqueue(
            worker_id=None,
            title="No Tools Required",
            objective="Think only",
            input={},
            required_capabilities=[],
        )

        scheduler = WorkloadScheduler(session)
        candidate = await scheduler.find_candidate(worker_id="w-base")
        assert candidate is not None
        assert candidate.id == task.id


async def test_node_affinity_filtering(
    database_url: str, session_factory: Any, profiles_dir: Path
) -> None:
    """Scenario 8: Task pre-assigned to node-a matches node-a and skips node-b."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker(session, profiles_dir, "w-node")
        wl = WorkloadService(session)

        task_node_a = await wl.enqueue(
            worker_id=None,
            title="Node A Pinned Task",
            objective="Must run on node-a",
            input={},
            assigned_node="node-a",
        )
        task_unpinned = await wl.enqueue(
            worker_id=None,
            title="Unpinned Task",
            objective="Any node",
            input={},
        )

        scheduler = WorkloadScheduler(session)

        # Runner on node-b sees only the unpinned task, skipping node-a pinned task
        candidate_b = await scheduler.find_candidate(worker_id="w-node", node_id="node-b")
        assert candidate_b is not None
        assert candidate_b.id == task_unpinned.id

        # Runner on node-a sees task_node_a (which was created earlier)
        candidate_a = await scheduler.find_candidate(worker_id="w-node", node_id="node-a")
        assert candidate_a is not None
        assert candidate_a.id == task_node_a.id


async def test_priority_overrides_fifo_creation_order(
    database_url: str, session_factory: Any, profiles_dir: Path
) -> None:
    """Scenario 9: Critical task created later is selected before older medium task."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker(session, profiles_dir, "w-prio")
        wl = WorkloadService(session)

        # Task 1: Medium, created earlier
        task_medium = await wl.enqueue(
            worker_id=None,
            title="Medium Task",
            objective="Normal work",
            input={},
            priority="medium",
        )

        # Task 2: Critical, created later
        task_critical = await wl.enqueue(
            worker_id=None,
            title="Critical Task",
            objective="Emergency patch",
            input={},
            priority="critical",
        )

        scheduler = WorkloadScheduler(session)
        candidate = await scheduler.find_candidate(worker_id="w-prio")
        assert candidate is not None
        assert candidate.id == task_critical.id
        assert candidate.id != task_medium.id


async def test_equal_priority_preserves_strict_fifo(
    database_url: str, session_factory: Any, profiles_dir: Path
) -> None:
    """Scenario 10: Two medium tasks are returned strictly in created_at ASC order."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker(session, profiles_dir, "w-fifo")
        wl = WorkloadService(session)

        t1 = await wl.enqueue(
            worker_id=None,
            title="Task 1",
            objective="First",
            input={},
            priority="medium",
        )
        t2 = await wl.enqueue(
            worker_id=None,
            title="Task 2",
            objective="Second",
            input={},
            priority="medium",
        )

        scheduler = WorkloadScheduler(session)
        candidates = await scheduler.find_candidates(worker_id="w-fifo", limit=2)
        assert len(candidates) == 2
        assert candidates[0].id == t1.id
        assert candidates[1].id == t2.id


async def test_incomplete_dependency_blocks_selection(
    database_url: str, session_factory: Any, profiles_dir: Path
) -> None:
    """Scenario 11: Task with incomplete or missing dependency is not selected."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker(session, profiles_dir, "w-dep")
        wl = WorkloadService(session)

        dep_task = await wl.enqueue(
            worker_id=None,
            title="Dep Task",
            objective="Prerequisite",
            input={},
        )
        dependent_task = await wl.enqueue(
            worker_id=None,
            title="Dependent Task",
            objective="Blocked on dep",
            input={},
            dependencies=[dep_task.id],
        )

        scheduler = WorkloadScheduler(session)
        # Only dep_task is runnable; dependent_task is blocked
        candidate = await scheduler.find_candidate(worker_id="w-dep")
        assert candidate is not None
        assert candidate.id == dep_task.id
        assert candidate.id != dependent_task.id

        # Change dep_task to in_progress -> dependent_task still blocked
        await session.execute(
            text("UPDATE tasks SET status = 'in_progress' WHERE id = :id"),
            {"id": dep_task.id},
        )
        await session.commit()

        assert await scheduler.find_candidate(worker_id="w-dep") is None


async def test_completed_dependency_unblocks_selection(
    database_url: str, session_factory: Any, profiles_dir: Path
) -> None:
    """Scenario 12: Dependent task becomes eligible once upstream task reaches completed."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker(session, profiles_dir, "w-dep2")
        wl = WorkloadService(session)

        dep_task = await wl.enqueue(
            worker_id=None,
            title="Dep Task",
            objective="Prerequisite",
            input={},
        )
        dependent_task = await wl.enqueue(
            worker_id=None,
            title="Dependent Task",
            objective="Blocked",
            input={},
            dependencies=[dep_task.id],
        )

        # Transition dep_task to completed
        await session.execute(
            text("UPDATE tasks SET status = 'completed' WHERE id = :id"),
            {"id": dep_task.id},
        )
        await session.commit()

        scheduler = WorkloadScheduler(session)
        candidate = await scheduler.find_candidate(worker_id="w-dep2")
        assert candidate is not None
        assert candidate.id == dependent_task.id


async def test_head_of_line_blocking_prevented(
    database_url: str, session_factory: Any, profiles_dir: Path
) -> None:
    """Scenario 13: Worker lacking skills for head task skips it and selects matching task."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker(session, profiles_dir, "w-python-only", skills=["python"])
        wl = WorkloadService(session)

        # Older task at head of queue requires 'rust'
        task_rust = await wl.enqueue(
            worker_id=None,
            title="Rust Head Task",
            objective="Compile Rust binary",
            input={},
            required_skills=["rust"],
        )

        # Newer task behind it requires 'python'
        task_python = await wl.enqueue(
            worker_id=None,
            title="Python Second Task",
            objective="Run script",
            input={},
            required_skills=["python"],
        )

        scheduler = WorkloadScheduler(session)
        candidate = await scheduler.find_candidate(worker_id="w-python-only")
        # HoL blocking prevented: skips Rust task and immediately returns Python task
        assert candidate is not None
        assert candidate.id == task_python.id
        assert candidate.id != task_rust.id


async def test_concurrent_runners_serialized_by_kernel_claim(
    database_url: str, session_factory: Any, profiles_dir: Path
) -> None:
    """Scenario 14: Two concurrent runners discovering same task produce 1 winner & 1 conflict."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session_setup:
        await _setup_worker(session_setup, profiles_dir, "runner-a")
        await _setup_worker(session_setup, profiles_dir, "runner-b")
        wl = WorkloadService(session_setup)
        task = await wl.enqueue(
            worker_id=None,
            title="Contested Task",
            objective="Two runners one prize",
            input={},
        )

    # Runner A and Runner B operate on independent sessions and independent registries
    async with session_factory() as session_a, session_factory() as session_b:
        reg_a = ProfileRegistry(session=session_a, profiles_dir=profiles_dir)
        reg_b = ProfileRegistry(session=session_b, profiles_dir=profiles_dir)

        adapter_a = CapturingAdapter(Result(status="completed", output={"runner": "a"}))
        adapter_b = CapturingAdapter(Result(status="completed", output={"runner": "b"}))

        kernel_a = WorkerExecutionKernel(
            session=session_a,
            adapter=adapter_a,
            resolve_context=reg_a.resolve_execution_context,
            worker_id="runner-a",
        )
        kernel_b = WorkerExecutionKernel(
            session=session_b,
            adapter=adapter_b,
            resolve_context=reg_b.resolve_execution_context,
            worker_id="runner-b",
        )

        orch_a = WorkerOrchestrator(session=session_a, kernel=kernel_a, worker_id="runner-a")
        orch_b = WorkerOrchestrator(session=session_b, kernel=kernel_b, worker_id="runner-b")

        # Concurrently execute process_one on both orchestrators
        res_a, res_b = await asyncio.gather(
            orch_a.process_one(),
            orch_b.process_one(),
        )

        # Exactly one succeeded and processed the task
        results = [res_a, res_b]
        assert results.count(True) == 1
        assert results.count(False) == 1

        total_processed = orch_a.metrics.tasks_processed + orch_b.metrics.tasks_processed
        assert total_processed == 1

        total_conflicts = orch_a.metrics.claim_conflicts + orch_b.metrics.claim_conflicts
        assert total_conflicts <= 1

        # Check DB state: task completed, assigned to the winning runner
        async with session_factory() as session_check:
            row = (
                await session_check.execute(
                    text("SELECT status, assigned_worker FROM tasks WHERE id = :id"),
                    {"id": task.id},
                )
            ).first()
            assert row is not None
            assert row.status == "completed"
            assert row.assigned_worker in ("runner-a", "runner-b")


async def test_candidate_selection_is_read_only(
    database_url: str, session_factory: Any, profiles_dir: Path
) -> None:
    """Scenario 15: Selection query produces zero mutations, locks, or audit events."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker(session, profiles_dir, "w-readonly", skills=["bash"])
        wl = WorkloadService(session)
        task = await wl.enqueue(
            worker_id=None,
            title="Read Only Task",
            objective="Query must not mutate",
            input={"key": "val"},
            required_skills=["bash"],
        )

        # Inspect database before selection
        before_task = (
            await session.execute(
                text(
                    "SELECT status, generation, assigned_worker, lock_expires_at, "
                    "started_at, completed_at FROM tasks WHERE id = :id"
                ),
                {"id": task.id},
            )
        ).first()
        before_events = (
            await session.execute(
                text("SELECT count(*) FROM task_events WHERE task_id = :id"),
                {"id": task.id},
            )
        ).scalar_one()

        # Execute candidate selection multiple times
        scheduler = WorkloadScheduler(session)
        for _ in range(3):
            cand = await scheduler.find_candidate(worker_id="w-readonly")
            assert cand is not None
            assert cand.id == task.id

        # Inspect database after selection
        after_task = (
            await session.execute(
                text(
                    "SELECT status, generation, assigned_worker, lock_expires_at, "
                    "started_at, completed_at FROM tasks WHERE id = :id"
                ),
                {"id": task.id},
            )
        ).first()
        after_events = (
            await session.execute(
                text("SELECT count(*) FROM task_events WHERE task_id = :id"),
                {"id": task.id},
            )
        ).scalar_one()

        assert before_task == after_task
        assert before_events == after_events
        assert after_task.status == "queued"
        assert after_task.assigned_worker is None
        assert after_task.generation == 0
        assert after_task.lock_expires_at is None
