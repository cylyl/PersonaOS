"""Quality evaluation engine for executed tasks.

Per ADR 0029 and docs/specs/task-telemetry-quality-latency-cost.md:
Computes an empirical quality score (0.0 to 1.0) based on task outcome,
retry counts, and output validity.
"""

from __future__ import annotations

from typing import Any


class QualityEvaluator:
    """Evaluates task execution quality and reliability."""

    def __init__(
        self,
        retry_penalty: float = 0.1,
        min_completed_score: float = 0.2,
    ) -> None:
        self.retry_penalty = retry_penalty
        self.min_completed_score = min_completed_score

    def evaluate(
        self,
        status: str,
        output: dict[str, Any] | None = None,
        error: str | None = None,
        retry_count: int = 0,
    ) -> float:
        """Compute normalized quality score between 0.0 and 1.0."""
        norm_status = status.lower().strip()

        if norm_status != "completed":
            # Failed or aborted tasks receive 0.0
            return 0.0

        # Base score for successful completion
        score = 1.0

        # Deduct penalty for prior failed retries before final success
        if retry_count > 0:
            score -= retry_count * self.retry_penalty

        # Check output payload completeness
        if not output:
            score -= 0.15
        else:
            # Check for error indicators in output
            if "error" in output or output.get("success") is False:
                score -= 0.3

        # Clamp score within valid bounds
        score = max(self.min_completed_score, min(1.0, score))
        return round(score, 4)
