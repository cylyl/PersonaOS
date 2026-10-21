"""Task routes — enqueue, inspect, list, review queue, and approval."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select, update

from personaos.api.deps import SessionDep, WorkloadDep
from personaos.api.schemas import (
    TaskActionRequest,
    TaskCheckpointResponse,
    TaskCreateRequest,
    TaskResponse,
)
from personaos.db.models import Task as TaskModel
from personaos.db.models import TaskCheckpoint
from personaos.domain.task import Task
from personaos.events.store import emit_task_event
from personaos.registry.profile import WorkerNotFoundError

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.post(
    "",
    response_model=TaskResponse,
    status_code=status.HTTP_201_CREATED,
)
async def enqueue_task(
    req: TaskCreateRequest,
    workload: WorkloadDep,
) -> Task:
    """Enqueue a new task for worker execution with atomic profile snapshot."""
    try:
        task = await workload.enqueue(
            worker_id=req.worker_id,
            title=req.title,
            objective=req.objective,
            input=req.input,
            required_skills=req.required_skills,
            required_capabilities=req.required_capabilities,
            priority=req.priority,
            type=req.type,
            dependencies=req.dependencies,
            deadline=req.deadline,
            max_retries=req.max_retries,
            profile_version=req.profile_version,
            side_effect_classification=req.side_effect_classification,
        )
        return task
    except WorkerNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.get("", response_model=list[TaskResponse])
async def list_tasks(
    session: SessionDep,
    task_status: Annotated[str | None, Query(alias="status")] = None,
    worker_id: Annotated[str | None, Query(alias="worker")] = None,
    priority: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[TaskModel]:
    """List tasks with optional status, worker, and priority filters."""
    query = select(TaskModel)
    if task_status:
        query = query.where(TaskModel.status == task_status)
    if worker_id:
        query = query.where(TaskModel.assigned_worker == worker_id)
    if priority:
        query = query.where(TaskModel.priority == priority)

    query = (
        query.order_by(TaskModel.created_at.desc(), TaskModel.id.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(query)
    return list(result.scalars().all())


@router.get("/review", response_model=list[TaskResponse])
async def list_review_tasks(
    session: SessionDep,
) -> list[TaskModel]:
    """List tasks currently in the review queue (status='review').

    Utilizes the ix_tasks_review_queue partial index (priority, created_at).
    """
    query = (
        select(TaskModel)
        .where(TaskModel.status == "review")
        .order_by(TaskModel.priority.desc(), TaskModel.created_at.asc())
    )
    result = await session.execute(query)
    return list(result.scalars().all())


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: str,
    session: SessionDep,
) -> TaskModel:
    """Get a task by its identifier."""
    result = await session.execute(
        select(TaskModel).where(TaskModel.id == task_id)
    )
    task = result.scalar_one_or_none()
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found",
        )
    return task


@router.get("/{task_id}/checkpoints", response_model=list[TaskCheckpointResponse])
async def list_task_checkpoints(
    task_id: str,
    session: SessionDep,
) -> list[TaskCheckpoint]:
    """List durable checkpoints for a task ordered chronologically."""
    task_res = await session.execute(
        select(TaskModel.id).where(TaskModel.id == task_id)
    )
    if task_res.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found",
        )

    result = await session.execute(
        select(TaskCheckpoint)
        .where(TaskCheckpoint.task_id == task_id)
        .order_by(TaskCheckpoint.created_at.asc())
    )
    return list(result.scalars().all())


@router.post("/{task_id}/approve", response_model=TaskResponse)
async def approve_task(
    task_id: str,
    session: SessionDep,
    req: TaskActionRequest | None = None,
) -> TaskModel:
    """Approve a task currently in review or blocked state.

    Re-queues the task for worker re-claim.
    """
    res = await session.execute(
        select(TaskModel).where(TaskModel.id == task_id)
    )
    task = res.scalar_one_or_none()
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found",
        )
    if task.status not in ("review", "blocked"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot approve task in state {task.status!r}; must be 'review' or 'blocked'",
        )

    actor = (req and req.actor_id) or "system:operator"
    await session.execute(
        update(TaskModel)
        .where(TaskModel.id == task_id)
        .values(status="queued")
    )
    await session.flush()
    task.status = "queued"

    # Emit task event
    await emit_task_event(
        session,
        task_id=task.id,
        event_type="task.created",
        actor_id=actor,
        payload={"action": "approved", "reason": req.reason if req else None},
    )

    return task


@router.post("/{task_id}/reject", response_model=TaskResponse)
async def reject_task(
    task_id: str,
    session: SessionDep,
    req: TaskActionRequest | None = None,
) -> TaskModel:
    """Reject a task currently in review or blocked state.

    Transitions status to 'failed'.
    """
    res = await session.execute(
        select(TaskModel).where(TaskModel.id == task_id)
    )
    task = res.scalar_one_or_none()
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found",
        )
    if task.status not in ("review", "blocked"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot reject task in state {task.status!r}; must be 'review' or 'blocked'",
        )

    actor = (req and req.actor_id) or "system:operator"
    reason = (req and req.reason) or "Rejected by operator"

    await session.execute(
        update(TaskModel)
        .where(TaskModel.id == task_id)
        .values(status="failed", last_error=reason)
    )
    await session.flush()
    task.status = "failed"
    task.last_error = reason

    await emit_task_event(
        session,
        task_id=task.id,
        event_type="task.failed",
        actor_id=actor,
        payload={"action": "rejected", "reason": reason},
    )

    return task
