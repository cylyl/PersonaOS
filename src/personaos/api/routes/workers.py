"""Worker routes — CRUD + profile versioning + quarantine."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select, update

from personaos.api.deps import RegistryDep, SessionDep
from personaos.api.schemas import (
    WorkerCreateRequest,
    WorkerDetailResponse,
    WorkerProfileCreateRequest,
    WorkerQuarantineRequest,
    WorkerResponse,
)
from personaos.db.models import Worker as WorkerModel
from personaos.domain.worker import Persona, WorkerProfile
from personaos.registry.profile import (
    ProfileError,
    ProfileVersionExistsError,
    ProfileVersionNotFoundError,
    WorkerNotFoundError,
)

router = APIRouter(prefix="/workers", tags=["workers"])


@router.get("", response_model=list[WorkerResponse])
async def list_workers(
    session: SessionDep,
    worker_status: Annotated[str | None, Query(alias="status")] = None,
    role: Annotated[str | None, Query()] = None,
) -> list[WorkerModel]:
    """List workers with optional status and role filtering."""
    query = select(WorkerModel)
    if worker_status:
        query = query.where(WorkerModel.status == worker_status)
    if role:
        query = query.where(WorkerModel.role == role)
    query = query.order_by(WorkerModel.id.asc())

    result = await session.execute(query)
    return list(result.scalars().all())


@router.post(
    "",
    response_model=WorkerResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_worker(
    req: WorkerCreateRequest,
    registry: RegistryDep,
    session: SessionDep,
) -> WorkerModel:
    """Register a new worker with initial profile (version 1)."""
    persona = Persona(
        style=req.persona.style,
        communication=req.persona.communication,
        autonomy=req.persona.autonomy,
        behavior=req.persona.behavior,
        goals=req.persona.goals,
        constraints=req.persona.constraints,
    )
    initial_profile = WorkerProfile(
        version=1,
        role=req.role,
        runtime=req.runtime,
        max_concurrent_tasks=req.max_concurrent_tasks,
        description=req.description,
        persona=persona,
        model=req.model,
        instructions=req.instructions,
        skills=req.skills,
        permissions=req.permissions,
    )

    try:
        await registry.register_worker(req.id, initial_profile)
    except ProfileError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    worker_orm = (
        await session.execute(select(WorkerModel).where(WorkerModel.id == req.id))
    ).scalar_one()
    return worker_orm


@router.get("/{worker_id}", response_model=WorkerDetailResponse)
async def get_worker(
    worker_id: str,
    registry: RegistryDep,
    session: SessionDep,
) -> WorkerDetailResponse:
    """Get worker details including profile metadata and available versions."""
    res = await session.execute(select(WorkerModel).where(WorkerModel.id == worker_id))
    worker = res.scalar_one_or_none()
    if worker is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Worker {worker_id} not found",
        )

    versions = registry.list_profile_versions(worker_id)
    current_profile_dict: dict[str, Any] | None = None
    try:
        prof = await registry.get_current_profile(worker_id)
        current_profile_dict = {
            "version": prof.version,
            "role": prof.role,
            "runtime": prof.runtime,
            "description": prof.description,
            "max_concurrent_tasks": prof.max_concurrent_tasks,
            "skills": prof.skills,
            "permissions": prof.permissions,
            "instructions": prof.instructions,
            "model": prof.model,
            "metadata": prof.metadata,
        }
    except ProfileVersionNotFoundError:
        current_profile_dict = None

    return WorkerDetailResponse(
        id=worker.id,
        name=worker.name,
        role=worker.role,
        status=worker.status,
        runtime=worker.runtime,
        max_concurrent_tasks=worker.max_concurrent_tasks,
        current_profile_version=worker.current_profile_version,
        description=worker.description,
        capabilities=worker.capabilities,
        permissions=worker.permissions,
        personality=worker.personality,
        memory_id=worker.memory_id,
        created_at=worker.created_at,
        updated_at=worker.updated_at,
        last_seen_at=worker.last_seen_at,
        current_profile=current_profile_dict,
        available_versions=versions,
    )


@router.get("/{worker_id}/profiles")
async def list_worker_profiles(
    worker_id: str,
    registry: RegistryDep,
) -> dict[str, Any]:
    """List available profile version numbers for a worker."""
    versions = registry.list_profile_versions(worker_id)
    return {"worker_id": worker_id, "versions": versions}


@router.get("/{worker_id}/profiles/{version}")
async def get_worker_profile_version(
    worker_id: str,
    version: int,
    registry: RegistryDep,
) -> dict[str, Any]:
    """Get a specific historical profile version."""
    try:
        prof = registry.get_profile_version(worker_id, version)
    except ProfileVersionNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    return {
        "version": prof.version,
        "role": prof.role,
        "runtime": prof.runtime,
        "description": prof.description,
        "max_concurrent_tasks": prof.max_concurrent_tasks,
        "skills": prof.skills,
        "permissions": prof.permissions,
        "instructions": prof.instructions,
        "model": prof.model,
        "metadata": prof.metadata,
    }


@router.post("/{worker_id}/profiles", status_code=status.HTTP_201_CREATED)
async def create_profile_version(
    worker_id: str,
    req: WorkerProfileCreateRequest,
    registry: RegistryDep,
) -> dict[str, Any]:
    """Create a new immutable profile version for a worker."""
    persona = Persona(
        style=req.persona.style,
        communication=req.persona.communication,
        autonomy=req.persona.autonomy,
        behavior=req.persona.behavior,
        goals=req.persona.goals,
        constraints=req.persona.constraints,
    )
    profile = WorkerProfile(
        version=req.version,
        role=req.role,
        runtime=req.runtime,
        max_concurrent_tasks=req.max_concurrent_tasks,
        description=req.description,
        persona=persona,
        model=req.model,
        instructions=req.instructions,
        skills=req.skills,
        permissions=req.permissions,
        metadata=req.metadata,
    )

    try:
        pv = await registry.create_profile_version(worker_id, profile)
        if req.activate:
            await registry.activate_profile_version(worker_id, req.version)
        return {
            "worker_id": worker_id,
            "version": pv.version,
            "activated": req.activate,
            "path": str(pv.path),
        }
    except WorkerNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ProfileVersionExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.post("/{worker_id}/activate-profile/{version}")
async def activate_profile_version(
    worker_id: str,
    version: int,
    registry: RegistryDep,
) -> dict[str, Any]:
    """Atomically activate an existing profile version."""
    try:
        await registry.activate_profile_version(worker_id, version)
        return {"worker_id": worker_id, "active_version": version}
    except WorkerNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ProfileVersionNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.post("/{worker_id}/quarantine", response_model=WorkerResponse)
async def quarantine_worker(
    worker_id: str,
    session: SessionDep,
    req: WorkerQuarantineRequest | None = None,
) -> WorkerModel:
    """Quarantine a worker (disallowing task execution)."""
    res = await session.execute(select(WorkerModel).where(WorkerModel.id == worker_id))
    worker = res.scalar_one_or_none()
    if worker is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Worker {worker_id} not found",
        )

    await session.execute(
        update(WorkerModel).where(WorkerModel.id == worker_id).values(status="quarantined")
    )
    await session.flush()
    worker.status = "quarantined"
    return worker


@router.post("/{worker_id}/unquarantine", response_model=WorkerResponse)
async def unquarantine_worker(
    worker_id: str,
    session: SessionDep,
) -> WorkerModel:
    """Lift quarantine from a worker (restoring status to idle)."""
    res = await session.execute(select(WorkerModel).where(WorkerModel.id == worker_id))
    worker = res.scalar_one_or_none()
    if worker is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Worker {worker_id} not found",
        )

    await session.execute(
        update(WorkerModel).where(WorkerModel.id == worker_id).values(status="idle")
    )
    await session.flush()
    worker.status = "idle"
    return worker
