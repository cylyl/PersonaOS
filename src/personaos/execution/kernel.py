"""WorkerExecutionKernel — orchestrates task execution lifecycle.

Per ADR 0010:
  - Kernel owns lifecycle: validate → in_progress → completed|failed
  - Uses an injected resolve_context callable (typically
    ProfileRegistry.resolve_execution_context)
  - Calls adapter.execute(context, checkpoint)
  - Persists result/error + emits task events
  - Independent of OpenClaw — uses the Adapter abstraction

Per ADR 0009:
  - Context is resolved from task.profile_version, never worker.current
  - Adapter receives the frozen context

v0.1 lifecycle:
  queued → in_progress → completed
  queued → in_progress → failed

No retries, leases, failover, or concurrent states yet — those require
execution-attempt modeling per ADR 0008 future evolution.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from personaos.db.models import Task as TaskModel
from personaos.domain.task import Task
from personaos.execution.adapters.base import Adapter
from personaos.execution.checkpoint import load as checkpoint_load
from personaos.execution.checkpoint import write as checkpoint_write
from personaos.execution.context import WorkerExecutionContext
from personaos.execution.result import Result
from personaos.events.store import emit_task_event


class TaskStatusError(Exception):
    """Raised when the task is not in 'queued' state for execution."""


# Type alias for the resolve-context callable.
# The kernel doesn't depend on ProfileRegistry directly — any callable
# that takes a Task and returns a WorkerExecutionContext works.
ResolveContext = Callable[[Task], Awaitable[WorkerExecutionContext]]


class WorkerExecutionKernel:
    """Orchestrates task execution lifecycle.

    Per ADR 0010:
      - Owns lifecycle: validate → in_progress → completed|failed
      - Uses injected resolve_context to load the snapshot
      - Calls adapter.execute(context, checkpoint)
      - Persists result/error + emits task events
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        adapter: Adapter,
        resolve_context: ResolveContext,
    ) -> None:
        self._session = session
        self._adapter = adapter
        self._resolve_context = resolve_context

    async def execute(self, task: Task) -> Result:
        """Execute a task end-to-end. Returns the Result.

        Per ADR 0009: context is resolved from task.profile_version, not worker.current.
        Per ADR 0010: lifecycle is queued → in_progress → completed|failed.

        Raises TaskStatusError if the task is not in 'queued' state (or doesn't exist).
        """
        # Phase 2: Resolve execution context (snapshot semantics).
        # Done BEFORE the atomic state transition so the adapter receives
        # the correct snapshot even if the transition fails.
        context = await self._resolve_context(task)

        # Phase 3: Atomic check-and-transition queued → in_progress + emit task.claimed.
        # The WHERE clause includes status='queued', so a stale or already-running
        # task causes 0 rows to be affected → TaskStatusError.
        try:
            update_result = await self._session.execute(
                update(TaskModel)
                .where(TaskModel.id == task.id, TaskModel.status == "queued")
                .values(status="in_progress", started_at=func.now())
            )
            if update_result.rowcount == 0:
                raise TaskStatusError(
                    f"task {task.id} is not in 'queued' state (or doesn't exist)"
                )
            await emit_task_event(
                self._session,
                task_id=task.id,
                event_type="task.claimed",
                actor_id="kernel",
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise

        # Phase 4: Load checkpoint (if any).
        checkpoint_state = await checkpoint_load(self._session, task.checkpoint_ref)

        # Phase 5: Execute via adapter (no DB transaction held while running).
        try:
            result = await self._adapter.execute(
                context=context, checkpoint=checkpoint_state
            )
        except Exception as e:
            # Adapter raised — convert to Result(failed). Kernel doesn't crash.
            result = Result(
                status="failed",
                error=f"{type(e).__name__}: {e}",
            )

        # Phase 6: Persist result + transition status + emit event.
        if result.status == "completed":
            await self._finish_completed(task, result)
        elif result.status == "failed":
            await self._finish_failed(task, result)
        else:
            raise ValueError(
                f"adapter returned unknown status {result.status!r}; "
                f"expected 'completed' or 'failed'"
            )

        return result

    async def _finish_completed(self, task: Task, result: Result) -> None:
        """Persist completed result: checkpoint (if any) + status + event."""
        try:
            checkpoint_ref = task.checkpoint_ref
            if result.new_checkpoint is not None:
                checkpoint_ref = await checkpoint_write(
                    self._session, task_id=task.id, state=result.new_checkpoint
                )
                await emit_task_event(
                    self._session,
                    task_id=task.id,
                    event_type="task.checkpointed",
                    actor_id="kernel",
                )

            await self._session.execute(
                update(TaskModel)
                .where(TaskModel.id == task.id)
                .values(
                    status="completed",
                    completed_at=func.now(),
                    result=result.output,
                    checkpoint_ref=checkpoint_ref,
                )
            )
            await emit_task_event(
                self._session,
                task_id=task.id,
                event_type="task.completed",
                actor_id="kernel",
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise

    async def _finish_failed(self, task: Task, result: Result) -> None:
        """Persist failed result: error + status + event."""
        try:
            await self._session.execute(
                update(TaskModel)
                .where(TaskModel.id == task.id)
                .values(
                    status="failed",
                    completed_at=func.now(),
                    result={"error": result.error} if result.error else None,
                    last_error=result.error,
                )
            )
            await emit_task_event(
                self._session,
                task_id=task.id,
                event_type="task.failed",
                actor_id="kernel",
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
