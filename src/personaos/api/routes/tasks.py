"""Task routes — enqueue, inspect, list, review queue, and approval."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from personaos.api.deps import (
    AuthenticatedOperatorDep,
    RegistryDep,
    SessionDep,
    WorkloadDep,
)
from personaos.api.schemas import (
    EventResponse,
    TaskApproveRequest,
    TaskCheckpointResponse,
    TaskCreateRequest,
    TaskRejectRequest,
    TaskResolveRequest,
    TaskResponse,
    TaskReviewContextResponse,
)
from personaos.db.models import Task as TaskModel
from personaos.db.models import TaskCheckpoint, TaskEvent
from personaos.domain.errors import (
    InvalidActionError,
    TaskNotFoundError,
    TaskStateConflictError,
    WorkerNotEligibleError,
)
from personaos.domain.task import Task
from personaos.registry.profile import WorkerNotFoundError
from personaos.workload.review import TaskReviewService

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


@router.get("/{task_id}/review-context", response_model=TaskReviewContextResponse)
async def get_task_review_context(
    task_id: str,
    session: SessionDep,
    operator: AuthenticatedOperatorDep,
    registry: RegistryDep,
) -> TaskReviewContextResponse:
    """Get aggregated diagnostic context for a task in the review queue."""
    task = (
        await session.execute(select(TaskModel).where(TaskModel.id == task_id))
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found",
        )

    worker = None
    is_outdated = False
    if task.assigned_worker:
        try:
            worker = await registry.get_worker(task.assigned_worker)
            if worker:
                is_outdated = worker.current_profile_version != task.profile_version
        except Exception:
            worker = None

    # Query recent events (bounded to last 20)
    events_res = await session.execute(
        select(TaskEvent)
        .where(TaskEvent.task_id == task_id)
        .order_by(TaskEvent.created_at.desc(), TaskEvent.id.desc())
        .limit(20)
    )
    recent_events = list(events_res.scalars().all())

    # Query checkpoints (bounded to last 5)
    cp_res = await session.execute(
        select(TaskCheckpoint)
        .where(TaskCheckpoint.task_id == task_id)
        .order_by(TaskCheckpoint.created_at.desc())
        .limit(5)
    )
    checkpoints = list(cp_res.scalars().all())

    cp_count_res = await session.execute(
        select(func.count(TaskCheckpoint.ref)).where(TaskCheckpoint.task_id == task_id)
    )
    checkpoints_count = cp_count_res.scalar_one()

    return TaskReviewContextResponse(
        task=TaskResponse.model_validate(task),
        assigned_worker_id=worker.id if worker else task.assigned_worker,
        worker_name=worker.name if worker else None,
        worker_role=worker.role if worker else None,
        worker_runtime=worker.runtime if worker else None,
        profile_version_snapshot=task.profile_version,
        worker_current_profile_version=worker.current_profile_version if worker else None,
        is_snapshot_outdated=is_outdated,
        execution_evidence={
            "last_error": task.last_error,
            "exhaustion_reason": task.exhaustion_reason,
            "diagnostics": task.result,
            "side_effect_classification": task.side_effect_classification,
        },
        recent_events=[EventResponse.model_validate(e) for e in recent_events],
        checkpoints_count=checkpoints_count,
        latest_checkpoint=(
            TaskCheckpointResponse.model_validate(checkpoints[0]) if checkpoints else None
        ),
        checkpoint_history=[TaskCheckpointResponse.model_validate(cp) for cp in checkpoints],
    )


@router.post("/{task_id}/approve", response_model=TaskResponse)
async def approve_task(
    task_id: str,
    session: SessionDep,
    operator: AuthenticatedOperatorDep,
    req: TaskApproveRequest,
) -> Task:
    """Approve a task currently in review state.

    Atomically re-queues the task with generation fencing and lease clearing.
    """
    review_svc = TaskReviewService(session)
    try:
        updated = await review_svc.approve(
            task_id=task_id,
            expected_generation=req.expected_generation,
            operator_id=operator.actor_id,
            reason=req.reason,
            reassign_worker_id=req.reassign_worker_id,
            reset_retries=req.reset_retries,
            max_retries=req.max_retries,
            attestation=req.attestation.model_dump() if req.attestation else None,
        )
        return updated
    except TaskNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except WorkerNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except TaskStateConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except (InvalidActionError, WorkerNotEligibleError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc


@router.post("/{task_id}/resolve", response_model=TaskResponse)
async def resolve_task(
    task_id: str,
    session: SessionDep,
    operator: AuthenticatedOperatorDep,
    req: TaskResolveRequest,
) -> Task:
    """Manually resolve a task currently in review state with verified output.

    Atomically transitions the task to 'completed' with generation fencing.
    """
    review_svc = TaskReviewService(session)
    try:
        updated = await review_svc.resolve(
            task_id=task_id,
            expected_generation=req.expected_generation,
            operator_id=operator.actor_id,
            reason=req.reason,
            verified_output=req.result,
        )
        return updated
    except TaskNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except TaskStateConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except InvalidActionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc


@router.post("/{task_id}/reject", response_model=TaskResponse)
async def reject_task(
    task_id: str,
    session: SessionDep,
    operator: AuthenticatedOperatorDep,
    req: TaskRejectRequest,
) -> Task:
    """Reject a task currently in review state.

    Atomically transitions status to 'failed' with generation fencing.
    """
    review_svc = TaskReviewService(session)
    try:
        updated = await review_svc.reject(
            task_id=task_id,
            expected_generation=req.expected_generation,
            operator_id=operator.actor_id,
            reason=req.reason,
        )
        return updated
    except TaskNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except TaskStateConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except InvalidActionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
