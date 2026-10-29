"""Distributed Heartbeat Agent with server-authoritative lease extension.

Per ADR 0034 — Remote Worker Daemon and Heartbeat Distribution:
Maintains node liveness and in-flight task leases with full jitter.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import Callable

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from personaos.execution.kernel import StaleGenerationConflictError

logger = structlog.get_logger(__name__)


class HeartbeatAgent:
    """Manages periodic jittered heartbeat emissions and task lease extensions."""

    def __init__(
        self,
        node_id: str,
        worker_id: str,
        session_factory: async_sessionmaker[AsyncSession],
        heartbeat_interval: float = 5.0,
        jitter_max: float = 1.0,
        lease_ttl_seconds: float = 30.0,
        on_stale_generation: Callable[[str, int], None] | None = None,
    ) -> None:
        self.node_id = node_id
        self.worker_id = worker_id
        self.session_factory = session_factory
        self.heartbeat_interval = heartbeat_interval
        self.jitter_max = jitter_max
        self.lease_ttl_seconds = lease_ttl_seconds
        self.on_stale_generation = on_stale_generation

        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._active_tasks: dict[str, int] = {}  # task_id -> expected_generation

    def track_task(self, task_id: str, generation: int) -> None:
        """Adds a task to the active heartbeat lease extension set."""
        self._active_tasks[task_id] = generation

    def untrack_task(self, task_id: str) -> None:
        """Removes a task from active heartbeat tracking."""
        self._active_tasks.pop(task_id, None)

    async def ping_once(self) -> None:
        """Executes a single atomic heartbeat and lease extension transaction."""
        async with self.session_factory() as session:
            # 1. Update worker last_seen_at
            await session.execute(
                text("UPDATE workers SET last_seen_at = NOW() WHERE id = :worker_id"),
                {"worker_id": self.worker_id},
            )

            # 2. Extend lease on all active tasks with generation check
            for task_id, gen in list(self._active_tasks.items()):
                lease_sql = text(
                    f"""
                    UPDATE tasks
                    SET lock_expires_at = NOW() + INTERVAL '{int(self.lease_ttl_seconds)} seconds'
                    WHERE id = :task_id
                      AND (assigned_node = :node_id OR assigned_node IS NULL)
                      AND generation = :expected_gen
                    RETURNING id
                    """
                )
                res = await session.execute(
                    lease_sql,
                    {"task_id": task_id, "node_id": self.node_id, "expected_gen": gen},
                )
                row = res.first()
                if row is None:
                    logger.error(
                        "FENCING MISMATCH: Task lease extension failed; generation is stale",
                        task_id=task_id,
                        expected_generation=gen,
                        node_id=self.node_id,
                    )
                    self.untrack_task(task_id)
                    if self.on_stale_generation:
                        self.on_stale_generation(task_id, gen)
                    raise StaleGenerationConflictError(
                        f"Task {task_id} generation {gen} is stale on node {self.node_id}"
                    )

            await session.commit()

    async def _loop(self) -> None:
        while self._running:
            # Full uniform jitter: sleep interval + uniform(0, jitter_max)
            sleep_duration = self.heartbeat_interval + random.uniform(0, self.jitter_max)
            try:
                await asyncio.sleep(sleep_duration)
                if not self._running:
                    break
                await self.ping_once()
            except StaleGenerationConflictError:
                pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Heartbeat emission failed", error=str(e), node_id=self.node_id)

    def start(self) -> None:
        """Starts the heartbeat agent background loop."""
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stops the heartbeat agent loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
