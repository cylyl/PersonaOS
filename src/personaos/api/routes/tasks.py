"""Task routes — enqueue, inspect, list, review queue, and approval."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select, update

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
        query.order_by(TaskModel.created_at.desc(), TaskModel.id.desc()).limit(limit).offset(offset)
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
    result = await session.execute(select(TaskModel).where(TaskModel.id == task_id))
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
    task_res = await session.execute(select(TaskModel.id).where(TaskModel.id == task_id))
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

    Atomically re-queues the task with generation fencing and lease clearing.
    """
    stmt = (
        update(TaskModel)
        .where(
            TaskModel.id == task_id,
            TaskModel.status.in_(["review", "blocked"]),
        )
        .values(
            status="queued",
            generation=TaskModel.generation + 1,
            lock_expires_at=None,
            assigned_worker=None,
        )
        .returning(TaskModel)
    )
    res = await session.execute(stmt)
    updated_task = res.scalar_one_or_none()
    if updated_task is None:
        existing = (
            await session.execute(select(TaskModel.status).where(TaskModel.id == task_id))
        ).scalar_one_or_none()
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Task {task_id} not found",
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot approve task in state {existing!r}; must be 'review' or 'blocked'",
        )

    actor = (req and req.actor_id) or "system:operator"
    await emit_task_event(
        session,
        task_id=updated_task.id,
        event_type="task.created",
        actor_id=actor,
        payload={"action": "approved", "reason": req.reason if req else None},
    )
    await session.flush()
    return updated_task


@router.post("/{task_id}/reject", response_model=TaskResponse)
async def reject_task(
    task_id: str,
    session: SessionDep,
    req: TaskActionRequest | None = None,
) -> TaskModel:
    """Reject a task currently in review or blocked state.

    Atomically transitions status to 'failed' with generation fencing.
    """
    actor = (req and req.actor_id) or "system:operator"
    reason = (req and req.reason) or "Rejected by operator"

    stmt = (
        update(TaskModel)
        .where(
            TaskModel.id == task_id,
            TaskModel.status.in_(["review", "blocked"]),
        )
        .values(
            status="failed",
            generation=TaskModel.generation + 1,
            last_error=reason,
            lock_expires_at=None,
            completed_at=func.now(),
        )
        .returning(TaskModel)
    )
    res = await session.execute(stmt)
    updated_task = res.scalar_one_or_none()
    if updated_task is None:
        existing = (
            await session.execute(select(TaskModel.status).where(TaskModel.id == task_id))
        ).scalar_one_or_none()
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Task {task_id} not found",
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot reject task in state {existing!r}; must be 'review' or 'blocked'",
        )

    await emit_task_event(
        session,
        task_id=updated_task.id,
        event_type="task.failed",
        actor_id=actor,
        payload={"action": "rejected", "reason": reason},
    )
    await session.flush()
    return updated_task
