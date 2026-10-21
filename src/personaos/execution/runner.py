"""Worker runner — the kernel main loop and lifecycle management.

Per VIKUNJA.md and ADR 0001:
Compute is replaceable; the worker is persistent.

The WorkerRunner binds the worker to a node, maintains periodic heartbeats,
drives the WorkerOrchestrator polling loop, and cleanly handles termination
signals (SIGINT, SIGTERM) to allow in-flight tasks to finish without losing state.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from personaos.db.models import Worker as WorkerModel
from personaos.db.models import WorkerNode
from personaos.orchestrator.worker import WorkerOrchestrator

logger = structlog.get_logger(__name__)


class WorkerRunner:
    """Manages the worker execution lifecycle.

    Owns:
      1. Node binding (creates WorkerNode record on startup).
      2. Background heartbeat task (periodically updates Worker.last_seen_at).
      3. Orchestrator driving the WorkerExecutionKernel.
      4. OS signal handling (SIGINT, SIGTERM) for graceful stop:
         - stops accepting new tasks
         - allows in-flight task to finish (via orchestrator.stop())
         - drains worker-node binding on exit.
    """

    def __init__(
        self,
        *,
        orchestrator: WorkerOrchestrator,
        session_factory: async_sessionmaker[AsyncSession],
        worker_id: str,
        node_id: str = "node-default",
        heartbeat_interval: float = 30.0,
    ) -> None:
        self._orchestrator = orchestrator
        self._session_factory = session_factory
        self._worker_id = worker_id
        self._node_id = node_id
        self._heartbeat_interval = heartbeat_interval
        self._stop_requested = asyncio.Event()
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._binding_id: str | None = None
        self._is_running = False

    @property
    def is_running(self) -> bool:
        """True if runner is currently running."""
        return self._is_running

    @property
    def binding_id(self) -> str | None:
        """Active worker-node binding ID."""
        return self._binding_id

    def stop(self) -> None:
        """Request graceful shutdown of the worker runner.

        Allows the currently running task (if any) to complete cleanly.
        """
        logger.info(
            "worker_runner_stop_requested",
            worker_id=self._worker_id,
            node_id=self._node_id,
        )
        self._stop_requested.set()
        self._orchestrator.stop()

    def _register_signals(self) -> None:
        """Register SIGINT and SIGTERM handlers for clean shutdown."""
        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self.stop)
        except (NotImplementedError, RuntimeError):
            # Not supported on non-main threads or some platforms
            pass

    async def _startup(self) -> None:
        """Bind worker to node and initiate background heartbeats."""
        self._register_signals()
        self._is_running = True
        self._binding_id = str(uuid.uuid4())

        async with self._session_factory() as session:
            binding = WorkerNode(
                id=self._binding_id,
                worker_id=self._worker_id,
                node_id=self._node_id,
                bound_at=datetime.now(UTC),
            )
            session.add(binding)
            await session.commit()

        logger.info(
            "worker_node_bound",
            binding_id=self._binding_id,
            worker_id=self._worker_id,
            node_id=self._node_id,
        )
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        """Background task periodically updating worker last_seen_at timestamp."""
        while not self._stop_requested.is_set():
            try:
                await asyncio.sleep(self._heartbeat_interval)
                if self._stop_requested.is_set():
                    break
                async with self._session_factory() as session:
                    await session.execute(
                        update(WorkerModel)
                        .where(WorkerModel.id == self._worker_id)
                        .values(last_seen_at=datetime.now(UTC))
                    )
                    await session.commit()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(
                    "worker_heartbeat_error",
                    worker_id=self._worker_id,
                    error=str(exc),
                )

    async def _shutdown(self) -> None:
        """Cancel heartbeat task and unbind worker from node."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task

        if self._binding_id:
            try:
                async with self._session_factory() as session:
                    await session.execute(
                        update(WorkerNode)
                        .where(WorkerNode.id == self._binding_id)
                        .values(unbound_at=datetime.now(UTC), reason="runner_shutdown")
                    )
                    await session.commit()
                logger.info("worker_node_unbound", binding_id=self._binding_id)
            except Exception as exc:
                logger.error("worker_node_drain_failed", error=str(exc))

        self._is_running = False

    async def run(self) -> None:
        """Main execution entrypoint. Runs orchestrator until stop is requested."""
        await self._startup()
        try:
            await self._orchestrator.run()
        finally:
            await self._shutdown()

    async def process_one(self) -> bool:
        """Process one runnable task via the orchestrator."""
        return await self._orchestrator.process_one()
