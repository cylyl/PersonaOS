"""Workload service — high-level operations on the durable queue.

v0.1 surface (Step 4):
  - enqueue(worker_id, ...) -> Task   (snapshot semantics, per ADR 0009)

Deferred to Step 5:
  - claim_next(worker_id, node_id) -> Task | None   # scheduler integration
  - complete(task_id, result)
  - fail(task_id, reason)            # retries if retry_count < max_retries; else -> failed
  - cancel(task_id, reason)
  - checkpoint(task_id, checkpoint_ref)

Per ADR 0009: enqueue snapshots workers.current_profile_version atomically
with the INSERT (SELECT ... FOR UPDATE in the same transaction). This is
the runtime-side mechanism that makes the snapshot column meaningful —
without it, the column is dead code and audit/replay becomes impossible.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from personaos.db.models import Task as TaskModel
from personaos.db.models import Worker as WorkerModel
from personaos.domain.task import Task
from personaos.events.store import emit_task_event
from personaos.registry.profile import WorkerNotFoundError
from personaos.workload.scheduler import WorkloadScheduler


class WorkloadService:
    """High-level workload operations.

    Per ADR 0009: enqueue() snapshots workers.current_profile_version
    atomically with the INSERT.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(
        self,
        *,
        worker_id: str | None = None,
        title: str,
        objective: str,
        input: dict[str, Any],  # noqa: A002
        required_skills: list[str] | None = None,
        required_capabilities: list[str] | None = None,
        priority: str = "medium",
        type: str | None = None,  # noqa: A002
        dependencies: list[str] | None = None,
        deadline: datetime | None = None,
        max_retries: int = 3,
        profile_version: int | None = None,
        side_effect_classification: str = "UNKNOWN",
        assigned_node: str | None = None,
    ) -> Task:
        """Enqueue a task; snapshot workers.current_profile_version atomically if worker_id given.

        Uses SELECT ... FOR UPDATE on the worker row + INSERT in the same
        transaction. This guarantees:

          - The profile_version snapshot is consistent with worker state at enqueue.
          - A concurrent activation cannot interleave between snapshot and INSERT.

        Raises WorkerNotFoundError if worker_id doesn't exist.
        """
        # Use the autobegin pattern (matches register_worker() in
        # registry/profile.py). This is robust against the session
        # having a prior open transaction — e.g. create_profile_version's
        # get_worker() read autobegins and doesn't commit. With explicit
        # begin() that pattern raises InvalidRequestError.
        try:
            if worker_id is not None:
                # 1. Lock the worker row + read current profile version
                #    (autobegins if no transaction, continues if one is open)
                result = await self._session.execute(
                    select(WorkerModel.current_profile_version)
                    .where(WorkerModel.id == worker_id)
                    .with_for_update()
                )
                worker_current_version = result.scalar_one_or_none()
                if worker_current_version is None:
                    raise WorkerNotFoundError(f"worker {worker_id} not found")

                assigned_version = (
                    profile_version if profile_version is not None else worker_current_version
                )
                actor_id = worker_id
            else:
                assigned_version = profile_version if profile_version is not None else 1
                actor_id = "workload"

            # 2. INSERT task with the snapshotted version (same transaction)
            task_row = TaskModel(
                id=str(uuid.uuid4()),
                title=title,
                objective=objective,
                input=input,
                assigned_worker=worker_id,
                assigned_node=assigned_node,
                profile_version=assigned_version,
                required_skills=list(required_skills or []),
                required_capabilities=list(required_capabilities or []),
                priority=priority,
                type=type,
                dependencies=list(dependencies or []),
                deadline=deadline,
                max_retries=max_retries,
                side_effect_classification=side_effect_classification,
                status="queued",
            )
            self._session.add(task_row)
            await self._session.flush()

            # Emit task.created event per ADR 0006 / ADR 0010
            await emit_task_event(
                self._session,
                task_id=task_row.id,
                event_type="task.created",
                actor_id=actor_id,
                payload={"title": title, "profile_version": assigned_version},
            )

            # 3. Commit — ends the autobegun transaction
            await self._session.commit()
        except Exception:
            # Roll back so the session is clean for the next operation
            await self._session.rollback()
            raise

        return Task.from_orm(task_row)

    async def next_runnable_for_worker(
        self,
        worker_id: str,
        node_id: str | None = None,
    ) -> Task | None:
        """Return the next runnable candidate matching worker skills, capabilities, and affinity.

        Delegates candidate selection to WorkloadScheduler (ADR 0019).
        Read-only; does not claim the task.
        """
        scheduler = WorkloadScheduler(self._session)
        return await scheduler.find_candidate(worker_id=worker_id, node_id=node_id)

    async def next_runnable(self) -> Task | None:
        """Return the next runnable candidate (FIFO), or None.

        Per ADR 0013 — a task is runnable iff:

          task.status == 'queued'
          AND
              task.dependencies is empty
              OR for every dep_id in task.dependencies, the task with
                 that id has status='completed'

        Ordering: ``created_at ASC, id ASC`` (deterministic FIFO).
        ``id`` is the tie-breaker for tasks created in the same
        microsecond.

        Read-only. Does NOT claim the task. The kernel still owns
        the atomic claim via Phase 3 (``UPDATE WHERE status='queued'``).
        Concurrent callers may receive the same task; the kernel's
        atomic claim ensures only one wins.

        Returns None when no task is runnable (empty queue, or all
        queued tasks have unsatisfied dependencies — including
        missing dep IDs or any dep with status != 'completed').

        Implementation note: the jsonb_array_elements_text expansion
        + LEFT JOIN against ``tasks dep`` is hard to express cleanly
        in pure SQLAlchemy ORM, so we use raw SQL via ``text()``.
        The query is read-only — no UPDATE/INSERT/DELETE.
        """
        # Raw SQL — the jsonb expansion + dep-existence check are
        # awkward to express in SQLAlchemy ORM. Read-only query;
        # kernel still owns the atomic claim.
        sql = text(
            """
            SELECT id, title, objective, status, priority, type,
                   required_skills, required_capabilities,
                   assigned_worker, assigned_node,
                   dependencies, input, checkpoint_ref, result,
                   retry_count, max_retries, last_error,
                   deadline, created_at, started_at, completed_at,
                   profile_version, lock_expires_at, generation,
                   side_effect_classification, exhaustion_reason
            FROM tasks
            WHERE status = 'queued'
              AND (
                    jsonb_array_length(dependencies) = 0
                    OR NOT EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements_text(dependencies) AS dep_id
                        LEFT JOIN tasks dep ON dep.id = dep_id
                        WHERE dep.id IS NULL              -- missing dep
                           OR dep.status <> 'completed'    -- dep not done
                    )
              )
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """
        )
        row = (await self._session.execute(sql)).first()
        if row is None:
            return None
        return self._row_to_task(row)

    async def find_stale_in_progress(self, limit: int = 10) -> list[Task]:
        """Find in-progress tasks whose leases have expired (lock_expires_at < NOW()).

        Per ADR 0016: Orphan-detection query.
        Returns tasks where status='in_progress', lock_expires_at is set,
        and lock_expires_at < NOW(), ordered by lock_expires_at ASC, id ASC.
        """
        sql = text(
            """
            SELECT id, title, objective, status, priority, type,
                   required_skills, required_capabilities,
                   assigned_worker, assigned_node,
                   dependencies, input, checkpoint_ref, result,
                   retry_count, max_retries, last_error,
                   deadline, created_at, started_at, completed_at,
                   profile_version, lock_expires_at, generation,
                   side_effect_classification, exhaustion_reason
            FROM tasks
            WHERE status = 'in_progress'
              AND lock_expires_at IS NOT NULL
              AND lock_expires_at < NOW()
            ORDER BY lock_expires_at ASC, id ASC
            LIMIT :limit
            """
        )
        res = await self._session.execute(sql, {"limit": limit})
        rows = res.fetchall()
        return [self._row_to_task(row) for row in rows]

    async def route_stale_to_review(
        self,
        task: Task,
        reason: str = "lease_expired_non_idempotent",
    ) -> bool:
        """Route an unclassified or side-effecting stale task to the review queue.

        Fenced by generation: only updates if task.generation matches DB state.
        Returns True if row was updated.
        """
        stmt = (
            update(TaskModel)
            .where(
                TaskModel.id == task.id,
                TaskModel.status == "in_progress",
                TaskModel.generation == task.generation,
                TaskModel.lock_expires_at < func.now(),
            )
            .values(
                status="review",
                generation=TaskModel.generation + 1,
                last_error=reason,
                lock_expires_at=None,
            )
        )
        res = await self._session.execute(stmt)
        rowcount = res.rowcount if isinstance(res, CursorResult) else getattr(res, "rowcount", 0)
        if rowcount > 0:
            await emit_task_event(
                self._session,
                task_id=task.id,
                event_type="task.failed",
                actor_id="recovery",
                payload={"action": "routed_to_review", "reason": reason},
            )
            await self._session.commit()
            return True
        return False

    async def claim_atomic(
        self,
        worker_id: str,
        node_id: str | None = None,
        cp_id: str | None = None,
        lease_ttl_seconds: int = 30,
    ) -> Task | None:
        """Atomically claim the next runnable task using PostgreSQL FOR UPDATE SKIP LOCKED.

        Per ADR 0033: Guarantees zero duplicate claims across concurrent control-plane instances.
        """
        sql = text(
            """
            WITH candidate AS (
                SELECT t.id
                FROM tasks t
                WHERE t.status = 'queued'
                  AND (t.assigned_worker IS NULL OR t.assigned_worker = :worker_id)
                  AND (t.deadline IS NULL OR t.deadline > NOW())
                  AND (
                      t.dependencies IS NULL
                      OR jsonb_array_length(t.dependencies) = 0
                      OR NOT EXISTS (
                          SELECT 1
                          FROM jsonb_array_elements_text(t.dependencies) AS dep_id
                          LEFT JOIN tasks dep ON dep.id = dep_id
                          WHERE dep.id IS NULL OR dep.status <> 'completed'
                      )
                  )
                ORDER BY
                    CASE t.priority
                        WHEN 'critical' THEN 4
                        WHEN 'high' THEN 3
                        WHEN 'medium' THEN 2
                        WHEN 'low' THEN 1
                        ELSE 2
                    END DESC,
                    t.created_at ASC,
                    t.id ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE tasks
            SET status = 'in_progress',
                assigned_worker = :worker_id,
                assigned_node = COALESCE(:node_id, tasks.assigned_node),
                claimed_by_cp_id = :cp_id,
                lock_expires_at = NOW() + (:lease_ttl || ' seconds')::interval,
                generation = tasks.generation + 1,
                started_at = NOW()
            FROM candidate
            WHERE tasks.id = candidate.id
            RETURNING tasks.id, tasks.title, tasks.objective, tasks.status,
                      tasks.priority, tasks.type, tasks.required_skills,
                      tasks.required_capabilities, tasks.assigned_worker, tasks.assigned_node,
                      tasks.dependencies, tasks.input, tasks.checkpoint_ref, tasks.result,
                      tasks.retry_count, tasks.max_retries, tasks.last_error,
                      tasks.deadline, tasks.created_at, tasks.started_at, tasks.completed_at,
                      tasks.profile_version, tasks.lock_expires_at, tasks.generation,
                      tasks.side_effect_classification, tasks.exhaustion_reason,
                      tasks.claimed_by_cp_id
            """
        )
        result = await self._session.execute(
            sql,
            {
                "worker_id": worker_id,
                "node_id": node_id,
                "cp_id": cp_id,
                "lease_ttl": str(lease_ttl_seconds),
            },
        )
        row = result.first()
        if row is None:
            return None
        await emit_task_event(
            self._session,
            task_id=row.id,
            event_type="task.claimed",
            actor_id=worker_id,
        )
        await self._session.commit()
        return self._row_to_task(row)

    @staticmethod
    def _row_to_task(row: Any) -> Task:
        return Task(
            id=row.id,
            title=row.title,
            objective=row.objective,
            status=row.status,
            priority=row.priority,
            type=row.type,
            required_skills=list(row.required_skills or []),
            required_capabilities=list(row.required_capabilities or []),
            assigned_worker=row.assigned_worker,
            assigned_node=row.assigned_node,
            dependencies=list(row.dependencies or []),
            input=row.input,
            checkpoint_ref=row.checkpoint_ref,
            result=row.result,
            retry_count=row.retry_count,
            max_retries=row.max_retries,
            last_error=row.last_error,
            deadline=row.deadline,
            created_at=row.created_at,
            started_at=row.started_at,
            completed_at=row.completed_at,
            profile_version=row.profile_version,
            lock_expires_at=row.lock_expires_at,
            generation=row.generation,
            side_effect_classification=row.side_effect_classification,
            exhaustion_reason=row.exhaustion_reason,
        )
