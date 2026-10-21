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

Per ADR 0016 (v0.2):
  - In-progress recovery with leases (lock_expires_at) and fencing (generation)
  - Fenced complete(), fail(), heartbeat(), and checkpoint_write()
  - Atomic reclaim() gated by side_effect_classification and retry limits
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from personaos.db.models import Task as TaskModel
from personaos.domain.task import Task
from personaos.domain.worker import Worker, WorkerProfile
from personaos.execution.adapters.base import Adapter
from personaos.execution.checkpoint import load as checkpoint_load
from personaos.execution.checkpoint import write as checkpoint_write
from personaos.execution.context import WorkerExecutionContext
from personaos.execution.result import Result
from personaos.events.store import emit_task_event
from personaos.registry.profile import ProfileRegistry


class TaskStatusError(Exception):
    """Raised when the task is not in an eligible state for the requested operation."""


class StaleWorkerError(Exception):
    """Raised when an operation (complete, fail, heartbeat, checkpoint_write) fails
    because ownership of the task has been transferred (fencing check failed).
    """


ResolveContext = Callable[[Task], Awaitable[WorkerExecutionContext]]


class WorkerExecutionKernel:
    """Orchestrates task execution lifecycle with v0.2 lease and fencing support."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        adapter: Adapter,
        resolve_context: Optional[ResolveContext] = None,
        worker_id: Optional[str] = None,
        profiles_dir: Optional[Path] = None,
        lease_ttl: timedelta = timedelta(minutes=15),
    ) -> None:
        self._session = session
        self._adapter = adapter
        if resolve_context is not None:
            self._resolve_context = resolve_context
        elif profiles_dir is not None:
            reg = ProfileRegistry(session=session, profiles_dir=profiles_dir)
            self._resolve_context = reg.resolve_execution_context
        else:
            async def _default_resolve(t: Task) -> WorkerExecutionContext:
                w = Worker(
                    id=self._worker_id or "worker",
                    name="Worker",
                    role="devops",
                    status="active",
                    runtime="openclaw",
                    max_concurrent_tasks=1,
                    current_profile_version=1,
                )
                p = WorkerProfile(
                    version=1,
                    role="devops",
                    runtime="openclaw",
                )
                return WorkerExecutionContext(worker=w, profile=p, task=t)
            self._resolve_context = _default_resolve

        self._worker_id = worker_id
        self._lease_ttl = lease_ttl
        self._generation: int = 0

    async def execute(self, task: Task) -> Result:
        """Execute a task end-to-end. Returns the Result."""
        context = await self._resolve_context(task)

        # Atomic claim: queued -> in_progress
        ttl_seconds = int(self._lease_ttl.total_seconds())
        update_vals = {
            "status": "in_progress",
            "started_at": func.now(),
            "generation": TaskModel.generation + 1,
            "lock_expires_at": func.now() + text(f"interval '{ttl_seconds} seconds'"),
        }
        if self._worker_id:
            update_vals["assigned_worker"] = self._worker_id

        try:
            update_result = await self._session.execute(
                update(TaskModel)
                .where(TaskModel.id == task.id, TaskModel.status == "queued")
                .values(**update_vals)
                .returning(TaskModel.generation, TaskModel.assigned_worker)
            )
            claimed = update_result.first()
            if claimed is None:
                raise TaskStatusError(
                    f"task {task.id} is not in 'queued' state (or doesn't exist)"
                )
            self._generation = claimed[0]
            if not self._worker_id and claimed[1]:
                self._worker_id = claimed[1]

            await emit_task_event(
                self._session,
                task_id=task.id,
                event_type="task.claimed",
                actor_id=self._worker_id or "kernel",
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise

        checkpoint_state = await checkpoint_load(self._session, task.checkpoint_ref)

        try:
            result = await self._adapter.execute(
                context=context, checkpoint=checkpoint_state
            )
        except Exception as e:
            result = Result(
                status="failed",
                error=f"{type(e).__name__}: {e}",
            )

        if result.status == "completed":
            await self.complete(task, result)
        elif result.status == "failed":
            await self.fail(task, result)
        else:
            raise ValueError(
                f"adapter returned unknown status {result.status!r}; "
                f"expected 'completed' or 'failed'"
            )

        return result

    async def complete(self, task: Task, result: Result) -> None:
        """Fenced completion: persists result + transitions to completed + clears lease."""
        expected_gen = self._generation if self._generation > 0 else getattr(task, "generation", 1)
        expected_worker = self._worker_id or task.assigned_worker

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
                    actor_id=expected_worker or "kernel",
                )

            stmt = (
                update(TaskModel)
                .where(
                    TaskModel.id == task.id,
                    TaskModel.generation == expected_gen,
                )
            )
            if expected_worker:
                stmt = stmt.where(TaskModel.assigned_worker == expected_worker)

            res = await self._session.execute(
                stmt.values(
                    status="completed",
                    completed_at=func.now(),
                    result=result.output,
                    checkpoint_ref=checkpoint_ref,
                    lock_expires_at=None,
                )
            )
            if res.rowcount == 0:
                raise StaleWorkerError(f"task {task.id} was reclaimed; write rejected")

            await emit_task_event(
                self._session,
                task_id=task.id,
                event_type="task.completed",
                actor_id=expected_worker or "kernel",
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise

    async def fail(self, task: Task, result: Result) -> None:
        """Fenced failure: persists error + transitions to failed + clears lease."""
        expected_gen = self._generation if self._generation > 0 else getattr(task, "generation", 1)
        expected_worker = self._worker_id or task.assigned_worker

        try:
            stmt = (
                update(TaskModel)
                .where(
                    TaskModel.id == task.id,
                    TaskModel.generation == expected_gen,
                )
            )
            if expected_worker:
                stmt = stmt.where(TaskModel.assigned_worker == expected_worker)

            res = await self._session.execute(
                stmt.values(
                    status="failed",
                    completed_at=func.now(),
                    result={"error": result.error} if result.error else None,
                    last_error=result.error,
                    lock_expires_at=None,
                )
            )
            if res.rowcount == 0:
                raise StaleWorkerError(f"task {task.id} was reclaimed; write rejected")

            await emit_task_event(
                self._session,
                task_id=task.id,
                event_type="task.failed",
                actor_id=expected_worker or "kernel",
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise

    async def heartbeat(self, task: Task) -> None:
        """Fenced heartbeat: refreshes lock_expires_at."""
        expected_gen = self._generation if self._generation > 0 else getattr(task, "generation", 1)
        expected_worker = self._worker_id or task.assigned_worker
        ttl_seconds = int(self._lease_ttl.total_seconds())

        try:
            stmt = (
                update(TaskModel)
                .where(
                    TaskModel.id == task.id,
                    TaskModel.status == "in_progress",
                    TaskModel.generation == expected_gen,
                )
            )
            if expected_worker:
                stmt = stmt.where(TaskModel.assigned_worker == expected_worker)

            res = await self._session.execute(
                stmt.values(
                    lock_expires_at=func.now() + text(f"interval '{ttl_seconds} seconds'")
                )
            )
            if res.rowcount == 0:
                raise StaleWorkerError(f"task {task.id} was reclaimed; heartbeat rejected")
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise

    async def checkpoint_write(self, task: Task, ref: str, state: dict) -> None:
        """Fenced checkpoint write: writes checkpoint state and updates checkpoint_ref."""
        expected_gen = self._generation if self._generation > 0 else getattr(task, "generation", 1)
        expected_worker = self._worker_id or task.assigned_worker

        try:
            await self._session.execute(
                text(
                    "INSERT INTO task_checkpoints (ref, task_id, state) "
                    "VALUES (:ref, :tid, CAST(:state AS jsonb))"
                ),
                {"ref": ref, "tid": task.id, "state": json.dumps(state)},
            )

            stmt = (
                update(TaskModel)
                .where(
                    TaskModel.id == task.id,
                    TaskModel.status == "in_progress",
                    TaskModel.generation == expected_gen,
                )
            )
            if expected_worker:
                stmt = stmt.where(TaskModel.assigned_worker == expected_worker)

            res = await self._session.execute(stmt.values(checkpoint_ref=ref))
            if res.rowcount == 0:
                raise StaleWorkerError(f"task {task.id} was reclaimed; checkpoint write rejected")
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise

    async def reclaim(self, task: Task) -> None:
        """Atomic recovery reclaim per ADR 0016 / spec."""
        row = (
            await self._session.execute(
                select(TaskModel).where(TaskModel.id == task.id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise TaskStatusError(f"Task {task.id} not found")

        # Side-effect classification safety gate
        if row.side_effect_classification != "IDEMPOTENT":
            raise TaskStatusError(
                f"Task {task.id} classification '{row.side_effect_classification}' is not IDEMPOTENT; auto-reclaim blocked"
            )

        # Retry exhaustion check
        if row.retry_count >= row.max_retries:
            exhaust_sql = text(
                """
                UPDATE tasks
                SET status = CASE
                      WHEN side_effect_classification = 'IDEMPOTENT' THEN 'failed'
                      WHEN side_effect_classification IN ('UNKNOWN', 'HAS_SIDE_EFFECTS') THEN 'review'
                    END,
                    last_error = COALESCE(last_error, 'max retries exceeded after crash recovery'),
                    exhaustion_reason = 'retry_limit_exhausted',
                    completed_at = CASE
                      WHEN side_effect_classification = 'IDEMPOTENT' THEN NOW()
                      ELSE NULL
                    END,
                    lock_expires_at = NULL
                WHERE id = :task_id
                  AND status = 'in_progress'
                  AND generation = :observed_gen
                  AND lock_expires_at < NOW()
                  AND retry_count >= max_retries
                  AND side_effect_classification IN ('IDEMPOTENT', 'UNKNOWN', 'HAS_SIDE_EFFECTS')
                RETURNING id
                """
            )
            res = await self._session.execute(
                exhaust_sql,
                {"task_id": task.id, "observed_gen": row.generation},
            )
            await self._session.commit()
            return

        # Atomic reclaim
        ttl_seconds = int(self._lease_ttl.total_seconds())
        worker_id_val = self._worker_id or "recovery-worker"

        reclaim_sql = text(
            f"""
            UPDATE tasks
            SET status = 'in_progress',
                assigned_worker = :worker_id,
                lock_expires_at = NOW() + interval '{ttl_seconds} seconds',
                generation = generation + 1,
                checkpoint_ref = NULL,
                started_at = NOW(),
                retry_count = retry_count + 1
            WHERE id = :task_id
              AND status = 'in_progress'
              AND generation = :observed_gen
              AND lock_expires_at < NOW()
              AND side_effect_classification = 'IDEMPOTENT'
              AND retry_count < max_retries
            RETURNING id, generation
            """
        )
        res = await self._session.execute(
            reclaim_sql,
            {
                "task_id": task.id,
                "worker_id": worker_id_val,
                "observed_gen": row.generation,
            },
        )
        claimed_row = res.first()
        if claimed_row is None:
            raise TaskStatusError(f"Task {task.id} could not be reclaimed (raced or not eligible)")

        self._generation = claimed_row.generation
        self._worker_id = worker_id_val
        await self._session.commit()

        # Recovery execution starts from scratch with checkpoint=None
        if self._adapter is not None:
            context = None
            if self._resolve_context is not None:
                context = await self._resolve_context(task)
            adapter_res = await self._adapter.execute(context=context, checkpoint=None)
            if adapter_res and adapter_res.output:
                await self._session.execute(
                    update(TaskModel)
                    .where(TaskModel.id == task.id, TaskModel.generation == self._generation)
                    .values(result=adapter_res.output)
                )
                await self._session.commit()
