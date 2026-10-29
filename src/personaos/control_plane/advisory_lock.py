"""PostgreSQL Transactional Advisory Locks for distributed singleton operations.

Per ADR 0033 — Multi-Node Control-Plane Distribution:
Advisory locks coordinate singleton periodic sweeps (such as orphan task reclamation,
dead node eviction, and telemetry rollups) across active-active control-plane instances
without requiring ZooKeeper or etcd.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

# Standard advisory lock identifiers (64-bit signed integers in PostgreSQL)
LOCK_ID_ORPHAN_REAPER: int = 100_001
LOCK_ID_NODE_SUPERVISOR: int = 100_002
LOCK_ID_TELEMETRY_AGGREGATOR: int = 100_003
LOCK_ID_CP_REAPER: int = 100_004


async def try_advisory_xact_lock(session: AsyncSession, lock_id: int) -> bool:
    """Attempts to acquire a transaction-scoped PostgreSQL advisory lock.

    Returns True if the lock was successfully acquired, False otherwise.
    The lock is automatically released when the transaction ends (commit or rollback).
    """
    result = await session.execute(
        text("SELECT pg_try_advisory_xact_lock(:lock_id)"),
        {"lock_id": lock_id},
    )
    acquired = result.scalar_one()
    return bool(acquired)


@asynccontextmanager
async def advisory_xact_lock(
    session: AsyncSession, lock_id: int
) -> AsyncIterator[bool]:
    """Async context manager for transaction-scoped advisory locks.

    Yields True if acquired, False if another instance holds the lock.
    """
    acquired = await try_advisory_xact_lock(session, lock_id)
    if not acquired:
        logger.debug("Advisory lock contention; lock held by another node", lock_id=lock_id)
    try:
        yield acquired
    finally:
        pass
