"""Process sandbox supervision and fail-closed termination.

Per ADR 0034 — Remote Worker Daemon and Heartbeat Distribution:
Manages isolated process groups and guarantees fail-closed termination
when a worker is fenced or cancelled.
"""

from __future__ import annotations

import asyncio
import os
import signal

import structlog

logger = structlog.get_logger(__name__)


class ProcessSandboxSupervisor:
    """Supervises isolated subprocess groups and enforces fail-closed termination."""

    def __init__(self) -> None:
        self._active_processes: dict[str, asyncio.subprocess.Process] = {}
        self._task_pgids: dict[str, int] = {}

    async def spawn_sandbox(
        self,
        task_id: str,
        cmd: list[str],
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> asyncio.subprocess.Process:
        """Spawns an execution sandbox in a new process group."""
        logger.info("Spawning execution sandbox", task_id=task_id, cmd=cmd)
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
            cwd=cwd,
            start_new_session=True,  # Creates a new process group with pgid == pid
        )
        self._active_processes[task_id] = proc
        self._task_pgids[task_id] = proc.pid
        return proc

    async def terminate_sandbox(
        self,
        task_id: str,
        grace_period_sec: float = 2.0,
    ) -> bool:
        """Gracefully terminates a process group with SIGTERM, escalating to SIGKILL."""
        proc = self._active_processes.get(task_id)
        pgid = self._task_pgids.get(task_id)
        if proc is None or pgid is None:
            return True

        if proc.returncode is not None:
            self._cleanup(task_id)
            return True

        logger.warn(
            "Terminating sandbox process group",
            task_id=task_id,
            pgid=pgid,
            grace_period=grace_period_sec,
        )

        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            self._cleanup(task_id)
            return True
        except Exception as e:
            logger.error("Failed to send SIGTERM to process group", pgid=pgid, error=str(e))

        try:
            await asyncio.wait_for(proc.wait(), timeout=grace_period_sec)
        except TimeoutError:
            logger.warn("Grace period expired; force killing process group", pgid=pgid)
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except Exception as e:
                logger.error("Failed to send SIGKILL to process group", pgid=pgid, error=str(e))
            try:
                async with asyncio.timeout(1.0):
                    await proc.wait()
            except TimeoutError:
                pass

        self._cleanup(task_id)
        return True

    async def fail_closed_abort(
        self, task_id: str, reason: str = "stale_generation_fenced"
    ) -> None:
        """Immediately terminates sandbox and purges state following a fencing failure."""
        logger.error(
            "FAIL-CLOSED ABORT: Fencing token rejected by control plane",
            task_id=task_id,
            reason=reason,
        )
        await self.terminate_sandbox(task_id, grace_period_sec=0.5)

    def _cleanup(self, task_id: str) -> None:
        self._active_processes.pop(task_id, None)
        self._task_pgids.pop(task_id, None)

    def is_active(self, task_id: str) -> bool:
        proc = self._active_processes.get(task_id)
        return proc is not None and proc.returncode is None
