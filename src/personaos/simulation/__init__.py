"""PersonaOS task simulation / replay.

Per ADR 0011 — Task Simulation / Replay (Step 6).

A simulation is a NEW task linked to a source task via ``simulated_from``.
It runs through the same ``WorkerExecutionKernel`` as production, only
the Adapter differs (``SimulationAdapter`` instead of ``OpenClawAdapter``).

Two submodules:
  - ``adapter``: SimulationAdapter (no-op, deterministic Result)
  - ``service``: SimulationService.replay(task_id) -> SimulationResult

Static invariant: this package must NOT import or reference
``OpenClawAdapter``, ``openclaw``, or ``OPENCLAW_API_URL``. The
``test_simulation_does_not_import_openclaw`` test enforces it.
"""

from personaos.simulation.adapter import SimulationAdapter
from personaos.simulation.service import (
    ReplayResult,
    ReplayService,
    SimulationResult,
    SimulationService,
    SourceTaskNotFoundError,
    TaskNotFoundError,
)

__all__ = [
    "ReplayResult",
    "ReplayService",
    "SimulationAdapter",
    "SimulationResult",
    "SimulationService",
    "SourceTaskNotFoundError",
    "TaskNotFoundError",
]
