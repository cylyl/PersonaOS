"""Common exceptions for external execution bridges."""

from __future__ import annotations


class IntegrationError(Exception):
    """Base exception for external host integration."""


class HostUnavailableError(IntegrationError):
    """Raised when the execution host or CLI is not reachable."""


class WorktreeNotFoundError(IntegrationError):
    """Raised when a requested workspace / worktree cannot be found."""


class AgentLaunchError(IntegrationError):
    """Raised when launching an agent process fails."""


class ExecutionTimeoutError(IntegrationError):
    """Raised when an external execution exceeds the allocated timeout."""


class ExecutionFailedError(IntegrationError):
    """Raised when an external execution fails with a non-zero exit code."""
