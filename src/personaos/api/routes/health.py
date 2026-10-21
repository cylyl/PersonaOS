"""Health route — DB connectivity + migration version."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException, status
from sqlalchemy import text

from personaos.api.deps import SessionDep
from personaos.api.schemas import HealthResponse

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/health", tags=["health"])


@router.get(
    "",
    response_model=HealthResponse,
    responses={503: {"model": HealthResponse, "description": "Service Degraded or Down"}},
)
async def check_health(session: SessionDep) -> HealthResponse:
    """Ping the database and report connectivity and migration status."""
    last_migration: str | None = None
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:
        logger.error("health_check_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "down",
                "db": "down",
                "last_migration": None,
                "version": "0.1.0",
            },
        ) from exc

    try:
        rev_res = await session.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
        row = rev_res.first()
        if row:
            last_migration = row[0]
    except Exception:
        # Migration table may not exist if unmigrated
        last_migration = None

    return HealthResponse(
        status="ok",
        db="ok",
        last_migration=last_migration,
        version="0.1.0",
    )
