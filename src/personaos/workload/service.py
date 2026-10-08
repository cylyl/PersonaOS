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
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from personaos.db.models import Task as TaskModel
from personaos.db.models import Worker as WorkerModel
from personaos.domain.task import Task
from personaos.registry.profile import WorkerNotFoundError


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
        worker_id: str,
        title: str,
        objective: str,
        input: dict,
        required_skills: Optional[list[str]] = None,
        required_capabilities: Optional[list[str]] = None,
        priority: str = "medium",
        type: Optional[str] = None,
        dependencies: Optional[list[str]] = None,
        deadline: Optional[datetime] = None,
        max_retries: int = 3,
    ) -> Task:
        """Enqueue a task; snapshot workers.current_profile_version atomically.

        Uses SELECT ... FOR UPDATE on the worker row + INSERT in the same
        transaction. This guarantees:

          - The profile_version snapshot is consistent with worker state at enqueue.
          - A concurrent activation cannot interleave between snapshot and INSERT.

        Raises WorkerNotFoundError if worker_id doesn't exist.

        TODO(Step 5): emit task.created event here (per ADR 0006).
        """
        # Use the autobegin pattern (matches register_worker() in
        # registry/profile.py). This is robust against the session
        # having a prior open transaction — e.g. create_profile_version's
        # get_worker() read autobegins and doesn't commit. With explicit
        # begin() that pattern raises InvalidRequestError.
        try:
            # 1. Lock the worker row + read current profile version
            #    (autobegins if no transaction, continues if one is open)
            result = await self._session.execute(
                select(WorkerModel.current_profile_version)
                .where(WorkerModel.id == worker_id)
                .with_for_update()
            )
            profile_version = result.scalar_one_or_none()
            if profile_version is None:
                raise WorkerNotFoundError(f"worker {worker_id} not found")

            # 2. INSERT task with the snapshotted version (same transaction)
            task_row = TaskModel(
                id=str(uuid.uuid4()),
                title=title,
                objective=objective,
                input=input,
                assigned_worker=worker_id,
                profile_version=profile_version,
                required_skills=list(required_skills or []),
                required_capabilities=list(required_capabilities or []),
                priority=priority,
                type=type,
                dependencies=list(dependencies or []),
                deadline=deadline,
                max_retries=max_retries,
                status="queued",
            )
            self._session.add(task_row)
            await self._session.flush()

            # 3. Commit — ends the autobegun transaction
            await self._session.commit()
        except Exception:
            # Roll back so the session is clean for the next operation
            await self._session.rollback()
            raise

        return Task.from_orm(task_row)
