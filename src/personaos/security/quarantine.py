"""Worker kill / quarantine domain service.

Per ADR 0004 & ADR 0020:
Transitions worker into 'quarantined' status, drains active node bindings,
and ensures scheduler / kernel prevent execution by quarantined workers.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from personaos.db.models import Worker as WorkerModel
from personaos.db.models import WorkerNode
from personaos.domain.errors import InvalidActionError
from personaos.registry.profile import WorkerNotFoundError

logger = structlog.get_logger(__name__)


async def quarantine_worker(
    session: AsyncSession,
    *,
    worker_id: str,
    reason: str,
    quarantined_by: str = "security:system",
) -> WorkerModel:
    """Quarantine a worker, revoking task execution eligibility and unbinding active nodes."""
    res = await session.execute(
        select(WorkerModel).where(WorkerModel.id == worker_id).with_for_update()
    )
    worker = res.scalar_one_or_none()
    if worker is None:
        raise WorkerNotFoundError(f"Worker {worker_id} not found")

    worker.status = "quarantined"
    worker.updated_at = func.now()

    # Drain active node bindings
    await session.execute(
        update(WorkerNode)
        .where(
            WorkerNode.worker_id == worker_id,
            WorkerNode.unbound_at.is_(None),
        )
        .values(
            unbound_at=func.now(),
            reason="worker_quarantined",
        )
    )

    logger.warn(
        "worker_quarantined",
        worker_id=worker_id,
        reason=reason,
        quarantined_by=quarantined_by,
    )
    await session.flush()
    return worker


async def unquarantine_worker(
    session: AsyncSession,
    *,
    worker_id: str,
    reason: str,
    restored_by: str = "security:system",
) -> WorkerModel:
    """Restore a quarantined worker to 'idle' status after review."""
    res = await session.execute(
        select(WorkerModel).where(WorkerModel.id == worker_id).with_for_update()
    )
    worker = res.scalar_one_or_none()
    if worker is None:
        raise WorkerNotFoundError(f"Worker {worker_id} not found")

    if worker.status != "quarantined":
        raise InvalidActionError(
            f"Worker {worker_id} is in status {worker.status!r}; expected 'quarantined'"
        )

    worker.status = "idle"
    worker.updated_at = func.now()

    logger.info(
        "worker_unquarantined",
        worker_id=worker_id,
        reason=reason,
        restored_by=restored_by,
    )
    await session.flush()
    return worker


def is_quarantined(worker: Any) -> bool:
    """Check if worker status is 'quarantined'."""
    if hasattr(worker, "status"):
        return bool(worker.status == "quarantined")
    if isinstance(worker, dict):
        return bool(worker.get("status") == "quarantined")
    return False
