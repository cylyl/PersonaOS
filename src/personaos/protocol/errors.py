"""Custom JSON-RPC 2.0 protocol error codes and exceptions.

Per ADR 0035 and docs/specs/cross-machine-agent-execution-protocol.md.
"""

from __future__ import annotations

from typing import Any


class PMAEPError(Exception):
    """Base exception for PersonaOS Cross-Machine Agent Execution Protocol."""

    code: int = -32000
    message: str = "PersonaOS protocol error"

    def __init__(self, message: str | None = None, data: Any = None) -> None:
        super().__init__(message or self.message)
        self.message = message or self.message
        self.data = data


class AuthenticationFailedError(PMAEPError):
    code = -32001
    message = "Authentication failed: invalid or expired credentials"


class PolicyViolationError(PMAEPError):
    code = -32003
    message = "Security policy violation: action or tool access denied"


class StaleGenerationProtocolError(PMAEPError):
    code = -32009
    message = "Fencing violation: task generation mismatch"


class CapacityExceededError(PMAEPError):
    code = -32015
    message = "Node capacity exceeded: no available execution slots"


class CheckpointCorruptedError(PMAEPError):
    code = -32020
    message = "Checkpoint payload corrupted or unreadable"
