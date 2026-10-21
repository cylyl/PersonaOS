"""Simulation and Replay Service.

Per ADR 0011 and docs/specs/simulation-and-replay.md:
  - Replays historical tasks deterministically using their snapshotted profile version
  - Evaluates counterfactual profile versions without mutating historical DB rows
  - Forks historical tasks into runnable tasks for the kernel
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from personaos.db.models import Task as TaskModel
from personaos.domain.task import Task
from personaos.execution.adapters.base import Adapter
from personaos.execution.adapters.simulation import SimulationAdapter
from personaos.execution.checkpoint import load as checkpoint_load
from personaos.execution.result import Result
from personaos.registry.profile import ProfileError, ProfileRegistry
from personaos.workload.service import WorkloadService


class TaskNotFoundError(Exception):
    """Raised when a task requested for replay cannot be found."""


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
    original_output: Optional[dict] = None
    replayed_output: Optional[dict] = None
    original_error: Optional[str] = None
    replayed_error: Optional[str] = None
    diff: dict[str, Any] = field(default_factory=dict)


class ReplayService:
    """Coordinates simulation and replay of tasks."""

    def __init__(
        self,
        session: AsyncSession,
        registry: ProfileRegistry,
        default_adapter: Optional[Adapter] = None,
    ) -> None:
        self._session = session
        self._registry = registry
        self._default_adapter = default_adapter or SimulationAdapter()

    async def replay(
        self,
        task_id: str,
        adapter: Optional[Adapter] = None,
        profile_version_override: Optional[int] = None,
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
            checkpoint_state = await checkpoint_load(
                self._session, task.checkpoint_ref
            )

        # Execute via adapter (catching any exception as failure)
        try:
            result = await active_adapter.execute(
                context=context, checkpoint=checkpoint_state
            )
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
        profile_version_override: Optional[int] = None,
        priority: Optional[str] = None,
    ) -> Task:
        """Create a new queued task for replay/simulation.

        Enqueues a new Task row with type='replay', copying the input,
        objective, and constraints from the historical task.
        """
        task_row = await self._session.get(TaskModel, task_id)
        if task_row is None:
            raise TaskNotFoundError(f"task {task_id} not found")

        if not task_row.assigned_worker:
            raise ProfileError(
                f"task {task_id} has no assigned_worker; cannot fork for replay"
            )

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
