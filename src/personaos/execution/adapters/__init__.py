"""Runtime adapters — thin shims around external agent runtimes.

Per ADR 0005: zero PersonaOS domain logic lives here. The Adapter Protocol
(base.py) defines the contract: execute(task, checkpoint) -> Result.
Concrete adapters:
- SimulationAdapter: Deterministic replay & test simulations.
- OpenClawAdapter: Agent Client Protocol (ACP) JSON-RPC 2.0 stdio bridge.
- LiteLLMAdapter: Live LLM execution via LiteLLM / OpenAI-compatible API gateway.
"""

from personaos.execution.adapters.base import Adapter
from personaos.execution.adapters.litellm import LiteLLMAdapter, LiteLLMConfig
from personaos.execution.adapters.openclaw import OpenClawAdapter
from personaos.execution.adapters.simulation import SimulationAdapter

__all__ = [
    "Adapter",
    "LiteLLMAdapter",
    "LiteLLMConfig",
    "OpenClawAdapter",
    "SimulationAdapter",
]
