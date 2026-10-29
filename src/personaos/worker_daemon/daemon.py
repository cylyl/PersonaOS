"""Remote Worker Daemon core orchestration.

Per ADR 0034 — Remote Worker Daemon and Heartbeat Distribution:
Autonomous daemon executing on remote host nodes, managing sandboxes,
issuing jittered heartbeats, and enforcing fail-closed termination.
"""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from personaos.worker_daemon.capabilities import SystemCapabilityScanner
from personaos.worker_daemon.config import DaemonConfig
from personaos.worker_daemon.heartbeat import HeartbeatAgent
from personaos.worker_daemon.idempotency import StepIdempotency
from personaos.worker_daemon.sandbox import ProcessSandboxSupervisor

logger = structlog.get_logger(__name__)


class WorkerDaemon:
    """Standalone daemon running on execution nodes."""

    def __init__(
        self,
        config: DaemonConfig,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.config = config
        self.session_factory = session_factory
        self.capabilities = SystemCapabilityScanner.scan()
        if config.extra_capabilities:
            self.capabilities.update(config.extra_capabilities)

        self.sandbox = ProcessSandboxSupervisor()
        self.heartbeat = HeartbeatAgent(
            node_id=config.node_id,
            worker_id=config.worker_id,
            session_factory=session_factory,
            heartbeat_interval=config.heartbeat_interval,
            jitter_max=config.heartbeat_jitter_max,
            lease_ttl_seconds=config.task_lease_ttl,
            on_stale_generation=self._handle_stale_generation,
        )

        self.status = "stopped"
        self._shutdown_event = asyncio.Event()

    def _handle_stale_generation(self, task_id: str, generation: int) -> None:
        """Callback invoked when control plane rejects a lease renewal due to stale generation."""
        logger.error(
            "FENCING TRIGGER: Local sandbox abort required",
            task_id=task_id,
            generation=generation,
        )
        asyncio.create_task(self.sandbox.fail_closed_abort(task_id))

    async def register(self) -> None:
        """Registers the worker node in PostgreSQL."""
        logger.info(
            "Registering worker daemon node",
            node_id=self.config.node_id,
            worker_id=self.config.worker_id,
        )
        async with self.session_factory() as session:
            # Bind worker to node
            await session.execute(
                text(
                    """
                    INSERT INTO worker_nodes (id, worker_id, node_id, bound_at)
                    VALUES (:id, :worker_id, :node_id, NOW())
                    """
                ),
                {
                    "id": f"bind-{self.config.node_id}",
                    "worker_id": self.config.worker_id,
                    "node_id": self.config.node_id,
                },
            )
            # Update worker status to idle / active
            await session.execute(
                text(
                    """
                    UPDATE workers
                    SET last_seen_at = NOW(),
                        status = 'idle'
                    WHERE id = :worker_id
                    """
                ),
                {"worker_id": self.config.worker_id},
            )
            await session.commit()

    async def start(self) -> None:
        """Starts the worker daemon and begins background heartbeats."""
        logger.info("Starting WorkerDaemon", node_id=self.config.node_id)
        await self.register()
        self.heartbeat.start()
        self.status = "ready"

    async def drain(self, reason: str = "runner_shutdown") -> None:
        """Enters drain mode: finishes active tasks and unbinds node."""
        logger.info("Draining worker daemon", node_id=self.config.node_id, reason=reason)
        self.status = "draining"

        # Wait up to drain_timeout for active tasks to complete
        start_drain = asyncio.get_event_loop().time()
        while self.heartbeat._active_tasks:
            if asyncio.get_event_loop().time() - start_drain > self.config.drain_timeout:
                logger.warn("Drain timeout exceeded; proceeding with shutdown")
                break
            await asyncio.sleep(0.5)

        # Unbind node
        async with self.session_factory() as session:
            await session.execute(
                text(
                    """
                    UPDATE worker_nodes
                    SET unbound_at = NOW(),
                        reason = :reason
                    WHERE id = :id AND unbound_at IS NULL
                    """
                ),
                {"id": f"bind-{self.config.node_id}", "reason": reason},
            )
            await session.commit()

        await self.heartbeat.stop()
        self.status = "offline"

    async def execute_task_step(
        self,
        task_id: str,
        generation: int,
        cmd: list[str],
        idempotency: StepIdempotency = StepIdempotency.STRICTLY_IDEMPOTENT,
    ) -> int:
        """Executes a command within an isolated sandbox and monitors heartbeats."""
        self.heartbeat.track_task(task_id, generation)
        try:
            proc = await self.sandbox.spawn_sandbox(task_id, cmd)
            exit_code = await proc.wait()
            return exit_code
        finally:
            self.heartbeat.untrack_task(task_id)
