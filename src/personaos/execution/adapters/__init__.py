"""Runtime adapters — thin shims around external agent runtimes.

Per ADR 0005: zero PersonaOS domain logic lives here. The Adapter Protocol
(base.py) defines the contract: execute(task, checkpoint) -> Result.
Concrete adapters (OpenClaw in v0.1; SimulationAdapter in Step 6; future local/cloud runtimes)
translate between runtime behavior and PersonaOS domain types.
"""

from personaos.execution.adapters.base import Adapter
from personaos.execution.adapters.openclaw import OpenClawAdapter
from personaos.execution.adapters.simulation import SimulationAdapter

__all__ = ["Adapter", "OpenClawAdapter", "SimulationAdapter"]
