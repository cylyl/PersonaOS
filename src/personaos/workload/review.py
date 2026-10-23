"""Task Review Resolution & Operator Re-Entry domain service.

Per ADR 0018: provides generation-fenced, atomic, and auditable transitions
for tasks in 'review' status.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from personaos.db.models import Task as TaskModel
from personaos.db.models import TaskCheckpoint
from personaos.db.models import Worker as WorkerModel
from personaos.domain.errors import (
    InvalidActionError,
    TaskNotFoundError,
    TaskStateConflictError,
    WorkerNotEligibleError,
)
from personaos.domain.task import Task
from personaos.events.store import emit_task_event
from personaos.registry.profile import WorkerNotFoundError


class TaskReviewService:
    """Domain operations for resolving quarantined tasks in the review queue."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def approve(
        self,
        *,
        task_id: str,
        expected_generation: int,
        operator_id: str,
        reason: str,
        reassign_worker_id: str | None = None,
        reset_retries: bool = True,
        max_retries: int | None = None,
        attestation: dict[str, Any] | None = None,
        resume_from_checkpoint: bool = True,
        checkpoint_ref_override: str | None = None,
    ) -> Task:
        """Approve and re-queue a task in 'review' status with generation fencing."""
        # 1. Fetch task under row-level lock
        task_row = (
            await self._session.execute(
                select(TaskModel).where(TaskModel.id == task_id).with_for_update()
            )
        ).scalar_one_or_none()

        if task_row is None:
            raise TaskNotFoundError(f"Task {task_id} not found")

        if task_row.status != "review":
            raise TaskStateConflictError(
                f"Task {task_id} is in status {task_row.status!r}; expected 'review'"
            )

        if task_row.generation != expected_generation:
            raise TaskStateConflictError(
                f"Task {task_id} generation mismatch: expected {expected_generation}, "
                f"found {task_row.generation}"
            )

        # 2. Check retry budget
        effective_max_retries = max_retries if max_retries is not None else task_row.max_retries
        if not reset_retries and task_row.retry_count >= effective_max_retries:
            raise InvalidActionError(
                f"Cannot approve task with reset_retries=False when retry limit is already "
                f"exhausted ({task_row.retry_count}/{effective_max_retries}). "
                f"Set reset_retries=True or increase max_retries."
            )

        # 3. Check classification attestation
        target_classification = task_row.side_effect_classification
        if attestation is not None:
            req_class = attestation.get("requested_classification")
            justification = attestation.get("safety_justification", "")
            if (
                req_class == "IDEMPOTENT"
                and task_row.side_effect_classification in ("UNKNOWN", "HAS_SIDE_EFFECTS")
                and (not justification or len(justification.strip()) < 15)
            ):
                raise InvalidActionError(
                    "Changing classification from non-idempotent to IDEMPOTENT requires an "
                    "explicit safety_justification of at least 15 characters."
                )
            if req_class:
                target_classification = req_class

        # 4. Check worker reassignment & snapshot consistency
        target_worker_id = task_row.assigned_worker
        target_profile_version = task_row.profile_version

        if reassign_worker_id is not None:
            worker = (
                await self._session.execute(
                    select(WorkerModel).where(WorkerModel.id == reassign_worker_id)
                )
            ).scalar_one_or_none()
            if worker is None:
                raise WorkerNotFoundError(f"Reassigned worker {reassign_worker_id} not found")
            if worker.status in ("retired", "quarantined"):
                raise WorkerNotEligibleError(
                    f"Worker {reassign_worker_id} is in status {worker.status!r}; "
                    f"cannot be assigned tasks"
                )
            target_worker_id = worker.id
            target_profile_version = worker.current_profile_version

        # 5. Archive prior diagnostics
        prior_result = task_row.result
        prior_error = task_row.last_error

        # Checkpoint handling per ADR 0021
        target_checkpoint_ref: str | None = None
        if checkpoint_ref_override is not None:
            cp_exists = (
                await self._session.execute(
                    select(TaskCheckpoint.ref).where(
                        TaskCheckpoint.ref == checkpoint_ref_override,
                        TaskCheckpoint.task_id == task_id,
                    )
                )
            ).scalar_one_or_none()
            if cp_exists is None:
                raise InvalidActionError(
                    f"Checkpoint {checkpoint_ref_override!r} does not exist for task {task_id}"
                )
            target_checkpoint_ref = checkpoint_ref_override
        elif resume_from_checkpoint:
            target_checkpoint_ref = task_row.checkpoint_ref
        else:
            target_checkpoint_ref = None

        # 6. Apply atomic update
        task_row.status = "queued"
        task_row.generation = task_row.generation + 1
        task_row.assigned_worker = target_worker_id
        task_row.profile_version = target_profile_version
        task_row.side_effect_classification = target_classification
        task_row.retry_count = 0 if reset_retries else task_row.retry_count
        if max_retries is not None:
            task_row.max_retries = max_retries
        task_row.lock_expires_at = None
        task_row.checkpoint_ref = target_checkpoint_ref
        task_row.exhaustion_reason = None
        task_row.result = None
        task_row.last_error = None

        # 7. Emit task.requeued event
        await emit_task_event(
            self._session,
            task_id=task_row.id,
            event_type="task.requeued",
            actor_id=operator_id,
            payload={
                "action": "approved_for_requeue",
                "reason": reason,
                "assigned_worker": target_worker_id,
                "profile_version": target_profile_version,
                "side_effect_classification": target_classification,
                "attestation": attestation,
                "prior_error": prior_error,
                "prior_diagnostics": prior_result,
                "resumed_from_checkpoint": target_checkpoint_ref,
            },
        )
        await self._session.flush()
        return Task.from_orm(task_row)

    async def resolve(
        self,
        *,
        task_id: str,
        expected_generation: int,
        operator_id: str,
        reason: str,
        verified_output: dict[str, Any],
    ) -> Task:
        """Manually mark a task in 'review' status as completed with verified output."""
        task_row = (
            await self._session.execute(
                select(TaskModel).where(TaskModel.id == task_id).with_for_update()
            )
        ).scalar_one_or_none()

        if task_row is None:
            raise TaskNotFoundError(f"Task {task_id} not found")

        if task_row.status != "review":
            raise TaskStateConflictError(
                f"Task {task_id} is in status {task_row.status!r}; expected 'review'"
            )

        if task_row.generation != expected_generation:
            raise TaskStateConflictError(
                f"Task {task_id} generation mismatch: expected {expected_generation}, "
                f"found {task_row.generation}"
            )

        composite_result: dict[str, Any] = {
            "verified_output": verified_output,
            "resolution_metadata": {
                "resolved_by": operator_id,
                "reason": reason,
                "resolved_at": datetime.now(UTC).isoformat(),
            },
            "prior_execution": {
                "last_error": task_row.last_error,
                "exhaustion_reason": task_row.exhaustion_reason,
                "prior_result": task_row.result,
                "prior_checkpoint_ref": task_row.checkpoint_ref,
                "prior_generation": task_row.generation,
            },
        }

        task_row.status = "completed"
        task_row.generation = task_row.generation + 1
        task_row.completed_at = datetime.now(UTC)
        task_row.result = composite_result
        task_row.lock_expires_at = None
        task_row.exhaustion_reason = None

        await emit_task_event(
            self._session,
            task_id=task_row.id,
            event_type="task.completed",
            actor_id=operator_id,
            payload={
                "action": "operator_resolved",
                "reason": reason,
                "verified_external": True,
            },
        )
        await self._session.flush()
        return Task.from_orm(task_row)

    async def reject(
        self,
        *,
        task_id: str,
        expected_generation: int,
        operator_id: str,
        reason: str,
    ) -> Task:
        """Terminally fail a task in 'review' status."""
        task_row = (
            await self._session.execute(
                select(TaskModel).where(TaskModel.id == task_id).with_for_update()
            )
        ).scalar_one_or_none()

        if task_row is None:
            raise TaskNotFoundError(f"Task {task_id} not found")

        if task_row.status != "review":
            raise TaskStateConflictError(
                f"Task {task_id} is in status {task_row.status!r}; expected 'review'"
            )

        if task_row.generation != expected_generation:
            raise TaskStateConflictError(
                f"Task {task_id} generation mismatch: expected {expected_generation}, "
                f"found {task_row.generation}"
            )

        task_row.status = "failed"
        task_row.generation = task_row.generation + 1
        task_row.completed_at = datetime.now(UTC)
        task_row.last_error = reason
        task_row.exhaustion_reason = "operator_rejected"
        task_row.lock_expires_at = None

        await emit_task_event(
            self._session,
            task_id=task_row.id,
            event_type="task.failed",
            actor_id=operator_id,
            payload={
                "action": "operator_rejected",
                "reason": reason,
            },
        )
        await self._session.flush()
        return Task.from_orm(task_row)
