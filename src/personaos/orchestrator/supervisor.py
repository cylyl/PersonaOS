"""Node lifecycle supervisor — heartbeat tracking, dead node detection, and eviction.

Per ADR 0001 & ADR 0024:
"Compute is replaceable; the worker is persistent."

Supervises worker-node bindings, detects dead or unreachable compute nodes whose
heartbeats have lapsed, unbinds stale nodes, and fast-expires locks on stranded
tasks so surviving or replacement compute can immediately resume and reclaim them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger(__name__)


@dataclass
class DeadNodeInfo:
    """Information regarding a detected dead worker node binding."""

    binding_id: str
    worker_id: str
    node_id: str


@dataclass
class SupervisionCycleResult:
    """Outcome of a single supervision cycle."""

    dead_nodes_evicted: int = 0
    tasks_expired: int = 0
    evicted_bindings: list[DeadNodeInfo] = field(default_factory=list)
    expired_task_ids: list[str] = field(default_factory=list)


class NodeSupervisor:
    """Supervises compute node liveness, detects dead nodes, and accelerates recovery."""

    def __init__(
        self,
        *,
        session: AsyncSession | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        default_heartbeat_timeout_seconds: float = 60.0,
    ) -> None:
        """Initialize the node supervisor.

        Args:
            session: Optional single AsyncSession (useful for unit tests and direct calls).
            session_factory: Optional session factory for background loops
                and isolated transactions.
            default_heartbeat_timeout_seconds: Default duration before an active binding is dead.
        """
        if session is None and session_factory is None:
            raise ValueError("Either session or session_factory must be provided to NodeSupervisor")
        self._session = session
        self._session_factory = session_factory
        self._default_heartbeat_timeout_seconds = default_heartbeat_timeout_seconds

    async def detect_dead_nodes(
        self,
        heartbeat_timeout_seconds: float | None = None,
        session: AsyncSession | None = None,
    ) -> list[DeadNodeInfo]:
        """Identify active worker-node bindings whose heartbeats have timed out.

        A node binding is considered dead if:
        1. unbound_at IS NULL (binding is currently marked active).
        2. AND either:
           - worker.last_seen_at IS NOT NULL and is older than cutoff.
           - worker.last_seen_at IS NULL and worker_nodes.bound_at is older than cutoff.
        """
        timeout = (
            heartbeat_timeout_seconds
            if heartbeat_timeout_seconds is not None
            else self._default_heartbeat_timeout_seconds
        )
        cutoff = datetime.now(UTC) - timedelta(seconds=timeout)

        sql = text(
            """
            SELECT wn.id, wn.worker_id, wn.node_id
            FROM worker_nodes wn
            JOIN workers w ON w.id = wn.worker_id
            WHERE wn.unbound_at IS NULL
              AND (
                (w.last_seen_at IS NOT NULL AND w.last_seen_at < :cutoff)
                OR
                (w.last_seen_at IS NULL AND wn.bound_at < :cutoff)
              )
            ORDER BY wn.bound_at ASC
            """
        )

        async def _query(s: AsyncSession) -> list[DeadNodeInfo]:
            result = await s.execute(sql, {"cutoff": cutoff})
            rows = result.fetchall()
            return [
                DeadNodeInfo(
                    binding_id=row.id,
                    worker_id=row.worker_id,
                    node_id=row.node_id,
                )
                for row in rows
            ]

        if session is not None:
            return await _query(session)
        if self._session is not None:
            return await _query(self._session)
        assert self._session_factory is not None
        async with self._session_factory() as sess:
            return await _query(sess)

    async def evict_dead_nodes(
        self,
        heartbeat_timeout_seconds: float | None = None,
        reason: str = "heartbeat_timeout",
        session: AsyncSession | None = None,
    ) -> SupervisionCycleResult:
        """Evict dead node bindings and accelerate lease expiration for stranded tasks.

        For each detected dead node:
        1. Unbinds the node in worker_nodes with the given reason.
        2. Fast-expires locks on any in_progress tasks assigned to the dead node,
           enabling surviving compute to immediately reclaim them.
        """

        async def _run(s: AsyncSession) -> SupervisionCycleResult:
            dead_nodes = await self.detect_dead_nodes(
                heartbeat_timeout_seconds=heartbeat_timeout_seconds,
                session=s,
            )
            if not dead_nodes:
                return SupervisionCycleResult()

            now = datetime.now(UTC)
            past = now - timedelta(seconds=1)
            evicted: list[DeadNodeInfo] = []
            all_expired_task_ids: list[str] = []

            for dead in dead_nodes:
                # 1. Unbind worker_nodes record
                unbind_sql = text(
                    """
                    UPDATE worker_nodes
                    SET unbound_at = :now, reason = :reason
                    WHERE id = :binding_id AND unbound_at IS NULL
                    """
                )
                await s.execute(
                    unbind_sql,
                    {"binding_id": dead.binding_id, "now": now, "reason": reason},
                )

                # 2. Fast-expire stranded in_progress tasks assigned to this node
                expire_tasks_sql = text(
                    """
                    UPDATE tasks
                    SET lock_expires_at = :past
                    WHERE status = 'in_progress'
                      AND assigned_node = :node_id
                      AND (lock_expires_at IS NULL OR lock_expires_at > :now)
                    RETURNING id
                    """
                )
                task_res = await s.execute(
                    expire_tasks_sql,
                    {"node_id": dead.node_id, "past": past, "now": now},
                )
                expired_ids = [r[0] for r in task_res.fetchall()]
                all_expired_task_ids.extend(expired_ids)
                evicted.append(dead)

                logger.info(
                    "node_evicted",
                    binding_id=dead.binding_id,
                    worker_id=dead.worker_id,
                    node_id=dead.node_id,
                    tasks_expired_count=len(expired_ids),
                    expired_task_ids=expired_ids,
                    reason=reason,
                )

            await s.commit()
            return SupervisionCycleResult(
                dead_nodes_evicted=len(evicted),
                tasks_expired=len(all_expired_task_ids),
                evicted_bindings=evicted,
                expired_task_ids=all_expired_task_ids,
            )

        if session is not None:
            return await _run(session)
        if self._session is not None:
            return await _run(self._session)
        assert self._session_factory is not None
        async with self._session_factory() as sess:
            return await _run(sess)

    async def supervise_cycle(
        self,
        heartbeat_timeout_seconds: float | None = None,
        session: AsyncSession | None = None,
    ) -> SupervisionCycleResult:
        """Run a single supervision cycle."""
        return await self.evict_dead_nodes(
            heartbeat_timeout_seconds=heartbeat_timeout_seconds,
            session=session,
        )

    async def run_loop(
        self,
        interval_seconds: float = 10.0,
        heartbeat_timeout_seconds: float | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """Run periodic supervision in the background until stopped."""
        logger.info(
            "node_supervisor_loop_started",
            interval_seconds=interval_seconds,
            heartbeat_timeout_seconds=heartbeat_timeout_seconds
            or self._default_heartbeat_timeout_seconds,
        )
        while stop_event is None or not stop_event.is_set():
            try:
                await self.supervise_cycle(heartbeat_timeout_seconds=heartbeat_timeout_seconds)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("node_supervisor_cycle_error", error=str(exc))

            try:
                if stop_event is not None:
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
                        break
                    except TimeoutError:
                        pass
                else:
                    await asyncio.sleep(interval_seconds)
            except asyncio.CancelledError:
                break

        logger.info("node_supervisor_loop_stopped")
