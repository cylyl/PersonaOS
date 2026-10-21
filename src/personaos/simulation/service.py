"""Simulation and Replay Service.

Per ADR 0011:
  - SimulationService: creates a NEW task linked to source via simulated_from and runs kernel
  - ReplayService: coordinates non-destructive simulation and counterfactual replay
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from personaos.db.models import Task as TaskModel
from personaos.domain.task import Task
from personaos.execution.adapters.base import Adapter, Result
from personaos.execution.adapters.simulation import SimulationAdapter
from personaos.execution.checkpoint import load as checkpoint_load
from personaos.execution.kernel import WorkerExecutionKernel
from personaos.registry.profile import ProfileError, ProfileRegistry
from personaos.workload.service import WorkloadService


class TaskNotFoundError(Exception):
    """Raised when a task requested for replay cannot be found."""


class SourceTaskNotFoundError(Exception):
    """Raised by SimulationService.replay when task_id doesn't exist."""


@dataclass(frozen=True)
class ReplayResult:
    """Outcome of a task replay or simulation run."""

    task_id: str
    original_profile_version: int
    executed_profile_version: int
    original_status: str
    replayed_status: str
    matched: bool
    replayed_result: Result
    original_output: dict | None = None
    replayed_output: dict | None = None
    original_error: str | None = None
    replayed_error: str | None = None
    diff: dict[str, Any] = field(default_factory=dict)


class ReplayService:
    """Coordinates simulation and replay of tasks."""

    def __init__(
        self,
        session: AsyncSession,
        registry: ProfileRegistry,
        default_adapter: Adapter | None = None,
    ) -> None:
        self._session = session
        self._registry = registry
        self._default_adapter = default_adapter or SimulationAdapter()

    async def replay(
        self,
        task_id: str,
        adapter: Adapter | None = None,
        profile_version_override: int | None = None,
    ) -> ReplayResult:
        """Replay a historical task non-destructively.

        Loads the historical task, reconstructs its execution context from
        its profile_version snapshot (or override), loads its initial checkpoint,
        executes the adapter, and compares the result.

        The database task row and task events remain unchanged.
        """
        task_row = await self._session.get(TaskModel, task_id)
        if task_row is None:
            raise TaskNotFoundError(f"task {task_id} not found")

        task = Task.from_orm(task_row)
        active_adapter = adapter or self._default_adapter

        # Resolve context using historical snapshot (or counterfactual override)
        version_to_use = profile_version_override or task.profile_version
        context = await self._registry.resolve_execution_context(
            task, profile_version_override=version_to_use
        )

        # Load initial checkpoint if any
        checkpoint_state = None
        if task.checkpoint_ref:
            checkpoint_state = await checkpoint_load(self._session, task.checkpoint_ref)

        # Execute via adapter (catching any exception as failure)
        try:
            result = await active_adapter.execute(context=context, checkpoint=checkpoint_state)
        except Exception as e:
            result = Result(status="failed", error=f"{type(e).__name__}: {e}")

        # Compare outcomes
        original_output = task_row.result
        original_error = task_row.last_error
        replayed_output = result.output
        replayed_error = result.error

        diff: dict[str, Any] = {}
        matched = True

        if task_row.status != result.status:
            matched = False
            diff["status"] = {
                "original": task_row.status,
                "replayed": result.status,
            }

        if original_output != replayed_output:
            matched = False
            diff["output"] = {
                "original": original_output,
                "replayed": replayed_output,
            }

        if original_error != replayed_error:
            matched = False
            diff["error"] = {
                "original": original_error,
                "replayed": replayed_error,
            }

        return ReplayResult(
            task_id=task.id,
            original_profile_version=task.profile_version,
            executed_profile_version=version_to_use,
            original_status=task_row.status,
            replayed_status=result.status,
            original_output=original_output,
            replayed_output=replayed_output,
            original_error=original_error,
            replayed_error=replayed_error,
            matched=matched,
            replayed_result=result,
            diff=diff,
        )

    async def fork_and_replay(
        self,
        task_id: str,
        *,
        profile_version_override: int | None = None,
        priority: str | None = None,
    ) -> Task:
        """Create a new queued task for replay/simulation.

        Enqueues a new Task row with type='replay', copying the input,
        objective, and constraints from the historical task.
        """
        task_row = await self._session.get(TaskModel, task_id)
        if task_row is None:
            raise TaskNotFoundError(f"task {task_id} not found")

        if not task_row.assigned_worker:
            raise ProfileError(f"task {task_id} has no assigned_worker; cannot fork for replay")

        target_version = (
            profile_version_override
            if profile_version_override is not None
            else task_row.profile_version
        )

        wl = WorkloadService(self._session)
        forked_task = await wl.enqueue(
            worker_id=task_row.assigned_worker,
            title=f"[Replay] {task_row.title}",
            objective=task_row.objective,
            input=dict(task_row.input),
            required_skills=list(task_row.required_skills or []),
            required_capabilities=list(task_row.required_capabilities or []),
            priority=priority or task_row.priority,
            type="replay",
            dependencies=list(task_row.dependencies or []),
            deadline=task_row.deadline,
            profile_version=target_version,
        )

        return forked_task


@dataclass(frozen=True)
class SimulationResult:
    """Outcome of a replay.

    Per ADR 0011:
      - task_id: the NEW simulation task id (different from source)
      - result: the adapter's output (SimulationAdapter returns a
        deterministic Result)
      - source_task_id: the source task id (immutable)
      - source_profile_version: the snapshot version used
    """

    task_id: str
    result: Result
    source_task_id: str
    source_profile_version: int


class SimulationService:
    """Replays a historical task without mutating it.

    Per ADR 0011: closes the loop on Steps 3-5. Uses the SAME
    WorkerExecutionKernel as production, only the Adapter differs.
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        kernel: WorkerExecutionKernel,
        registry: ProfileRegistry,
    ) -> None:
        self._session = session
        self._kernel = kernel
        self._registry = registry

    async def replay(self, task_id: str) -> SimulationResult:
        """Replay the historical task.

        Creates a NEW task with simulated_from=task_id. The source task
        is NEVER mutated. Returns SimulationResult with the new task id,
        the adapter's Result, and the source linkage.

        Raises:
            SourceTaskNotFoundError: if task_id doesn't exist.
        """
        source_row = (
            await self._session.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one_or_none()
        if source_row is None:
            raise SourceTaskNotFoundError(f"source task {task_id} not found")

        source_profile_version = source_row.profile_version
        source_assigned_worker = source_row.assigned_worker
        source_input = copy.deepcopy(source_row.input) if source_row.input else {}
        source_title = source_row.title
        source_objective = source_row.objective
        source_required_skills = list(source_row.required_skills or [])
        source_required_capabilities = list(source_row.required_capabilities or [])
        source_priority = source_row.priority
        source_deadline = source_row.deadline

        sim_row = TaskModel(
            id=str(uuid.uuid4()),
            title=source_title,
            objective=source_objective,
            input=source_input,
            assigned_worker=source_assigned_worker,
            profile_version=source_profile_version,
            required_skills=source_required_skills,
            required_capabilities=source_required_capabilities,
            priority=source_priority,
            type="simulation",
            dependencies=[],
            simulated_from=task_id,
            status="queued",
            max_retries=3,
            retry_count=0,
            result=None,
            last_error=None,
            checkpoint_ref=None,
            assigned_node=None,
            started_at=None,
            completed_at=None,
            deadline=source_deadline,
        )
        self._session.add(sim_row)
        await self._session.flush()

        sim_task = Task.from_orm(sim_row)
        result = await self._kernel.execute(sim_task)

        return SimulationResult(
            task_id=sim_row.id,
            result=result,
            source_task_id=task_id,
            source_profile_version=source_profile_version,
        )
