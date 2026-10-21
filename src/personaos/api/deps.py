"""Dependency injection helpers for FastAPI."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from personaos.registry.profile import ProfileRegistry
from personaos.workload.service import WorkloadService


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield an AsyncSession from the application session factory.

    Commits on successful request completion; rolls back on unhandled error.
    """
    factory: async_sessionmaker[AsyncSession] | None = getattr(
        request.app.state, "session_factory", None
    )
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database session factory not configured",
        )

    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_profile_registry(
    session: SessionDep,
) -> ProfileRegistry:
    """Dependency provider for ProfileRegistry."""
    return ProfileRegistry(session)


RegistryDep = Annotated[ProfileRegistry, Depends(get_profile_registry)]


def get_workload_service(
    session: SessionDep,
) -> WorkloadService:
    """Dependency provider for WorkloadService."""
    return WorkloadService(session)


WorkloadDep = Annotated[WorkloadService, Depends(get_workload_service)]


def get_current_worker(
    x_worker_id: Annotated[str | None, Header(alias="X-Worker-Id")] = None,
) -> str | None:
    """Header-based worker identity for v0.1 control plane."""
    return x_worker_id


CurrentWorkerDep = Annotated[str | None, Depends(get_current_worker)]
