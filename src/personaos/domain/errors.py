"""Domain-level errors.

Raised by services in registry/, workload/, security/, execution/.
Caught at the API boundary (api/deps.py, api/routes/*) and translated to
HTTP responses.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base exception for all PersonaOS domain errors."""


class TaskNotFoundError(DomainError):
    """Raised when a requested task does not exist."""


class TaskStateConflictError(DomainError):
    """Raised when a task is in an unexpected status or generation."""


class InvalidActionError(DomainError):
    """Raised when an operation is invalid given task policy or parameters."""


class WorkerNotEligibleError(DomainError):
    """Raised when a worker is not in an eligible state for task assignment."""


class PermissionDeniedError(DomainError):
    """Raised when an action or tool invocation is denied by security policy."""


class SecurityPolicyError(DomainError):
    """Raised when security policy evaluation encounters an invalid state or configuration."""


class SecretNotFoundError(DomainError):
    """Raised when a referenced secret is not found in environment or secret store."""
