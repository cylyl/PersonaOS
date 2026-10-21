"""SimulationAdapter — deterministic no-op adapter for replay testing.

Per ADR 0011 / Step 6:

The point of ``SimulationAdapter`` is NOT to model realistic worker
behavior. It is to prove the simulation plumbing works:

  source task (DB row)
    ↓
  SimulationService.replay (snapshot profile_version + input)
    ↓
  WorkerExecutionKernel.execute (resolve context from snapshot)
    ↓
  SimulationAdapter.execute (here)
    ↓
  Result (deterministic, derived from context)
    ↓
  DB row on the new sim task

A real "replay the actual OpenClaw run" adapter is a future capability
(model-pinned / recording / deterministic). For v0.1, the strongest
claim is:

  > Same historical inputs + profile produce the same execution
  > configuration (worker id, profile version, persona, instructions,
  > runtime config), and SimulationAdapter proves the complete
  > ORM → domain → profile → kernel → result chain.

Static isolation: this module MUST NOT import ``OpenClawAdapter``,
``openclaw``, or ``OPENCLAW_API_URL``. Enforced by
``tests/integration/test_task_simulation.py::test_simulation_does_not_import_openclaw``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from personaos.execution.context import WorkerExecutionContext
from personaos.execution.result import Result

if TYPE_CHECKING:
    from personaos.execution.adapters.base import Adapter


class SimulationAdapter:
    """Deterministic no-op adapter. Always returns a 'completed' Result.

    The output echoes the bits of context that prove the snapshot contract
    held end-to-end:

      - ``simulated``: marker so the DB row is identifiable as a sim run
      - ``task_id``: the simulation task's id (the NEW task)
      - ``profile_version``: the profile version the kernel actually loaded
        (== source.profile_version, NOT worker.current)

    If the kernel is wired correctly, ``profile_version`` here will equal
    the source's snapshot even after the worker's current profile has
    moved on. That is the chain test's proof.
    """

    async def execute(
        self,
        context: WorkerExecutionContext,
        checkpoint: Any | None = None,
    ) -> Result:
        """Return a deterministic Result derived from the context.

        The kernel guarantees ``context.profile.version`` matches what
        the source task snapshotted at enqueue (per ADR 0009). The adapter
        simply echoes it so the chain test can verify it.
        """
        return Result(
            status="completed",
            output={
                "simulated": True,
                "task_id": context.task.id,
                "profile_version": context.profile.version,
            },
        )


# Protocol conformance marker (mypy-only)
_: Adapter = SimulationAdapter  # type: ignore[type-arg,assignment]
