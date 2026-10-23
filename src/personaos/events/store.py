"""Task event store — append-only event emission.

Per ADR 0006 & ADR 0020: events are append-only. State tables remain source of truth.

Emits task event types (CHECK-constrained):
  task.created, task.claimed, task.checkpointed, task.completed, task.failed,
  task.requeued, policy.allowed, policy.denied, approval.requested

The CHECK constraint on task_events.type enforces valid values at the
DB level. This module enforces them at the Python level too (faster
feedback, clearer error messages).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from personaos.db.models import TaskEvent

_TASK_EVENT_TYPES = (
    "task.created",
    "task.claimed",
    "task.checkpointed",
    "task.completed",
    "task.failed",
    "task.requeued",
    "policy.allowed",
    "policy.denied",
    "approval.requested",
)


async def emit_task_event(
    session: AsyncSession,
    *,
    task_id: str,
    event_type: str,
    actor_id: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Emit a task lifecycle or security event.

    The event row is INSERTed into task_events. The caller is responsible
    for the surrounding transaction (commit/rollback).

    Raises ValueError if event_type is not one of the allowed types.
    """
    if event_type not in _TASK_EVENT_TYPES:
        raise ValueError(f"event_type must be one of {_TASK_EVENT_TYPES}, got {event_type!r}")
    await session.execute(
        insert(TaskEvent).values(
            task_id=task_id,
            type=event_type,
            actor_id=actor_id,
            payload=payload or {},
        )
    )
