"""Subprocess crash worker helper for real OS process crash tests.

Per ADR 0016 and Step 13b:
Spawns as an independent OS process, establishes node binding, claims a task,
writes an intermediate checkpoint, executes an external side effect,
emits READY_FOR_KILL to stdout, and sleeps until SIGKILL is received.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from personaos.execution.context import WorkerExecutionContext
from personaos.execution.kernel import WorkerExecutionKernel
from personaos.execution.result import Result
from personaos.execution.runner import WorkerRunner
from personaos.orchestrator.worker import WorkerOrchestrator
from personaos.registry.profile import ProfileRegistry


class CrashSimulationAdapter:
    """Adapter that performs a side effect, writes a checkpoint, and halts."""

    def __init__(
        self,
        *,
        side_effect_file: Path,
        checkpoint_ref: str,
        kernel: WorkerExecutionKernel,
    ) -> None:
        self.side_effect_file = side_effect_file
        self.checkpoint_ref = checkpoint_ref
        self.kernel = kernel

    async def execute(
        self,
        context: WorkerExecutionContext,
        checkpoint: Any | None = None,
    ) -> Result:
        # 1. External side effect
        with open(self.side_effect_file, "a", encoding="utf-8") as f:
            f.write(f"side_effect_attempt:{context.task.id}:step1\n")

        # 2. Intermediate checkpoint
        await self.kernel.checkpoint_write(
            context.task,
            self.checkpoint_ref,
            {"step": "phase_1_done", "progress": 50},
        )

        # 3. Notify parent test that state is committed and we are ready for SIGKILL
        sys.stdout.write("READY_FOR_KILL\n")
        sys.stdout.flush()

        # 4. Sleep to await SIGKILL from parent
        await asyncio.sleep(60.0)

        return Result(status="completed", output={"done": True})


async def async_main() -> None:
    if len(sys.argv) < 7:
        sys.stderr.write(
            "Usage: crash_worker.py <db_url> <worker_id> <node_id> <task_id> "
            "<profiles_dir> <side_effect_file> [lease_ttl_s]\n"
        )
        sys.exit(1)

    db_url = sys.argv[1]
    worker_id = sys.argv[2]
    node_id = sys.argv[3]
    profiles_dir = Path(sys.argv[5])
    side_effect_file = Path(sys.argv[6])
    lease_ttl_s = float(sys.argv[7]) if len(sys.argv) > 7 else 2.0

    engine = create_async_engine(db_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        registry = ProfileRegistry(session=session, profiles_dir=profiles_dir)

        # Create dummy kernel first to pass reference to adapter
        kernel: WorkerExecutionKernel = WorkerExecutionKernel(
            session=session,
            adapter=None,  # type: ignore[arg-type]
            worker_id=worker_id,
            resolve_context=registry.resolve_execution_context,
            lease_ttl=timedelta(seconds=lease_ttl_s),
            session_factory=session_factory,
        )
        adapter = CrashSimulationAdapter(
            side_effect_file=side_effect_file,
            checkpoint_ref="cp-crash-001",
            kernel=kernel,
        )
        kernel._adapter = adapter

        orchestrator = WorkerOrchestrator(session=session, kernel=kernel, poll_interval=0.05)
        runner = WorkerRunner(
            orchestrator=orchestrator,
            session_factory=session_factory,
            worker_id=worker_id,
            node_id=node_id,
        )

        # Run one task (will claim, execute adapter, notify parent, and sleep)
        await runner._startup()
        try:
            await orchestrator.process_one()
        finally:
            await runner._shutdown()
            await engine.dispose()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
