"""Cross-Machine Agent Execution Protocol (PMAEP) package.

Per ADR 0035 and docs/specs/cross-machine-agent-execution-protocol.md.
"""

from personaos.protocol.errors import (
    AuthenticationFailedError,
    CapacityExceededError,
    CheckpointCorruptedError,
    PMAEPError,
    PolicyViolationError,
    StaleGenerationProtocolError,
)
from personaos.protocol.models import (
    METHOD_AGENT_CANCEL,
    METHOD_AGENT_CHECKPOINT,
    METHOD_AGENT_COMPLETE,
    METHOD_AGENT_DISPATCH,
    METHOD_AGENT_HANDSHAKE,
    METHOD_AGENT_HEARTBEAT,
    RPCError,
    RPCRequest,
    RPCResponse,
    TraceMetadata,
)
from personaos.protocol.tokens import TaskTokenManager

__all__ = [
    "PMAEPError",
    "AuthenticationFailedError",
    "PolicyViolationError",
    "StaleGenerationProtocolError",
    "CapacityExceededError",
    "CheckpointCorruptedError",
    "TraceMetadata",
    "RPCRequest",
    "RPCResponse",
    "RPCError",
    "TaskTokenManager",
    "METHOD_AGENT_HANDSHAKE",
    "METHOD_AGENT_DISPATCH",
    "METHOD_AGENT_HEARTBEAT",
    "METHOD_AGENT_CHECKPOINT",
    "METHOD_AGENT_CANCEL",
    "METHOD_AGENT_COMPLETE",
]
