"""Task entity — durable unit of work.

Eight lifecycle states: queued, assigned, in_progress, blocked, review,
completed, failed, canceled. State machine enforced by execution/runner.py
and workload/service.py.

Per ADR 0008: tasks has 8 lifecycle states + priority enum (low/medium/high/critical).
Per ADR 0009: profile_version is captured at enqueue, never updated.

Source of truth: schemas/task.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Optional


_TASK_STATUSES = (
    "queued",
    "assigned",
    "in_progress",
    "blocked",
    "review",
    "completed",
    "failed",
    "canceled",
)
_TASK_PRIORITIES = ("low", "medium", "high", "critical")


if TYPE_CHECKING:
    from personaos.db.models import Task as TaskModel


@dataclass(frozen=True)
class Task:
    """Domain object for a unit of work.

    Per ADR 0009: profile_version is the snapshot of the worker's
    profile version at enqueue time. It is NEVER updated.

    Status / priority are plain strings (not enums) — the DB CHECK
    constraints enforce valid values; we keep the dataclass round-trip
    trivial so Task.from_orm works without enum conversion overhead.
    """

    id: str
    title: str
    objective: str
    input: dict
    profile_version: int
    status: str
    priority: str
    assigned_worker: Optional[str] = None
    assigned_node: Optional[str] = None
    required_skills: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    result: Optional[dict] = None
    retry_count: int = 0
    max_retries: int = 3
    last_error: Optional[str] = None
    deadline: Optional[datetime] = None
    type: Optional[str] = None
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.status not in _TASK_STATUSES:
            raise ValueError(
                f"status must be one of {_TASK_STATUSES}, got {self.status!r}"
            )
        if self.priority not in _TASK_PRIORITIES:
            raise ValueError(
                f"priority must be one of {_TASK_PRIORITIES}, got {self.priority!r}"
            )
        if self.profile_version < 1:
            raise ValueError(
                f"profile_version must be >= 1, got {self.profile_version}"
            )

    @classmethod
    def from_orm(cls, task_row: "TaskModel") -> "Task":
        """Build a Task dataclass from a TaskModel ORM row.

        Per ADR 0009: profile_version is preserved verbatim from the row.
        This is the one place where an ORM row becomes a domain object;
        everything downstream of WorkloadService.enqueue operates on Task.
        """
        return cls(
            id=task_row.id,
            title=task_row.title,
            objective=task_row.objective,
            input=task_row.input,
            profile_version=task_row.profile_version,
            status=task_row.status,
            priority=task_row.priority,
            assigned_worker=task_row.assigned_worker,
            assigned_node=task_row.assigned_node,
            required_skills=list(task_row.required_skills or []),
            required_capabilities=list(task_row.required_capabilities or []),
            dependencies=list(task_row.dependencies or []),
            result=task_row.result,
            retry_count=task_row.retry_count,
            max_retries=task_row.max_retries,
            last_error=task_row.last_error,
            deadline=task_row.deadline,
            type=task_row.type,
            created_at=task_row.created_at,
            started_at=task_row.started_at,
            completed_at=task_row.completed_at,
        )
