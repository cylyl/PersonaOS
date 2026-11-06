"""Structured exceptions for Orca execution integration."""

from __future__ import annotations

from personaos.integrations.common.errors import (
    ExecutionFailedError,
    ExecutionTimeoutError,
    HostUnavailableError,
    IntegrationError,
    WorktreeNotFoundError,
)


class OrcaError(IntegrationError):
    """Base exception for all Orca bridge operations."""


class OrcaUnavailableError(OrcaError, HostUnavailableError):
    """Raised when Orca CLI binary is missing or Orca runtime server is unreachable."""


class OrcaTimeoutError(OrcaError, ExecutionTimeoutError):
    """Raised when an Orca CLI operation or terminal execution exceeds the timeout."""


class OrcaWorktreeError(OrcaError, WorktreeNotFoundError):
    """Raised when an Orca repository or worktree cannot be found or resolved."""


class OrcaCLIError(OrcaError):
    """Raised when an Orca CLI command returns a non-zero exit code or an ok=false payload."""

    def __init__(self, message: str, exit_code: int = 1, error_payload: dict | None = None) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.error_payload = error_payload or {}


class OrcaExecutionFailedError(OrcaError, ExecutionFailedError):
    """Raised when an executed command or agent in Orca fails with non-zero exit status."""

    def __init__(self, message: str, exit_code: int = 1, terminal_output: str = "") -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.terminal_output = terminal_output
