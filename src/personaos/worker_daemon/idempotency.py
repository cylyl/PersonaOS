"""Step and task idempotency classification for execution side-effect safety.

Per ADR 0016, ADR 0021, and ADR 0034.
"""

from __future__ import annotations

from enum import StrEnum


class StepIdempotency(StrEnum):
    """Classification of side-effect safety for a task or execution step."""

    STRICTLY_IDEMPOTENT = "strictly_idempotent"
    """Safe to replay completely from start (e.g. read, compile, pure computation)."""

    CHECKPOINT_IDEMPOTENT = "checkpoint_idempotent"
    """Safe to replay strictly from the latest committed checkpoint forward."""

    NON_IDEMPOTENT = "non_idempotent"
    """Produces external side effects (e.g. payments, cloud resource creation).

    If compute fails mid-flight, MUST be quarantined in 'review' status rather
    than retried automatically.
    """

    @classmethod
    def from_classification(cls, classification: str) -> StepIdempotency:
        """Maps legacy side_effect_classification string to StepIdempotency."""
        c = (classification or "").upper()
        if c == "IDEMPOTENT":
            return cls.STRICTLY_IDEMPOTENT
        return cls.NON_IDEMPOTENT
