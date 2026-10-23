"""Human approval gate and operator sign-off service.

Per ADR 0004, ADR 0018, & ADR 0020:
When policy evaluates to REQUIRE_APPROVAL:
  1. request_approval(session, task_id, reason, ...) moves task to 'review' with
     exhaustion_reason='approval_required' and emits 'approval.requested'.
  2. Generation fencing prevents races.
  3. Operator can approve via /tasks/{id}/approve (delegates to TaskReviewService.approve)
     which re-queues the task, or reject via /tasks/{id}/reject which fails it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from personaos.db.models import Task as TaskModel
from personaos.domain.errors import InvalidActionError, TaskNotFoundError, TaskStateConflictError
from personaos.domain.task import Task
from personaos.events.store import emit_task_event
from personaos.workload.review import TaskReviewService


async def request_approval(
    session: AsyncSession,
    *,
    task_id: str,
    reason: str,
    approvers: list[str] | None = None,
    policy_id: str | None = None,
    rule_id: str | None = None,
    operator_id: str = "security:policy",
    expected_generation: int | None = None,
) -> Task:
    """Transition a task to 'review' status awaiting human operator approval."""
    task_row = (
        await session.execute(select(TaskModel).where(TaskModel.id == task_id).with_for_update())
    ).scalar_one_or_none()

    if task_row is None:
        raise TaskNotFoundError(f"Task {task_id} not found")

    if expected_generation is not None and task_row.generation != expected_generation:
        raise TaskStateConflictError(
            f"Task {task_id} generation mismatch: expected {expected_generation}, "
            f"found {task_row.generation}"
        )

    if task_row.status not in ("queued", "in_progress", "assigned", "review"):
        raise InvalidActionError(
            f"Cannot request approval for task {task_id} in status {task_row.status!r}"
        )

    task_row.status = "review"
    task_row.generation = task_row.generation + 1
    task_row.exhaustion_reason = "approval_required"
    task_row.last_error = reason
    task_row.lock_expires_at = None

    effective_approvers = approvers or ["system:operator"]

    await emit_task_event(
        session,
        task_id=task_row.id,
        event_type="approval.requested",
        actor_id=operator_id,
        payload={
            "reason": reason,
            "policy_id": policy_id,
            "rule_id": rule_id,
            "approvers": effective_approvers,
            "generation": task_row.generation,
        },
    )

    await session.flush()
    return Task.from_orm(task_row)


async def approve_approval(
    session: AsyncSession,
    *,
    task_id: str,
    operator_id: str,
    expected_generation: int,
    reason: str = "approved by operator",
    reassign_worker_id: str | None = None,
    reset_retries: bool = True,
    max_retries: int | None = None,
    attestation: dict[str, Any] | None = None,
) -> Task:
    """Approve a gated task using TaskReviewService to atomically re-enqueue it."""
    service = TaskReviewService(session)
    return await service.approve(
        task_id=task_id,
        expected_generation=expected_generation,
        operator_id=operator_id,
        reason=reason,
        reassign_worker_id=reassign_worker_id,
        reset_retries=reset_retries,
        max_retries=max_retries,
        attestation=attestation,
    )


async def reject_approval(
    session: AsyncSession,
    *,
    task_id: str,
    operator_id: str,
    expected_generation: int,
    reason: str = "rejected by operator",
) -> Task:
    """Reject a gated task using TaskReviewService to terminally fail it."""
    service = TaskReviewService(session)
    return await service.reject(
        task_id=task_id,
        expected_generation=expected_generation,
        operator_id=operator_id,
        reason=reason,
    )
