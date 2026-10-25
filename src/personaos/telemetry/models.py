"""Telemetry data models and aggregation representations.

Per ADR 0029 and docs/specs/task-telemetry-quality-latency-cost.md:
Data structures for task telemetry, worker analytics, and workload distribution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class TaskTelemetry:
    """Telemetry metrics captured for an individual task execution."""

    task_id: str
    worker_id: str | None = None
    node_id: str | None = None
    model_name: str | None = None
    status: str = "completed"
    quality_score: float = 1.0
    queue_wait_ms: float = 0.0
    execution_duration_ms: float = 0.0
    total_latency_ms: float = 0.0
    retry_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    recorded_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert telemetry to serializable dictionary."""
        return {
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "node_id": self.node_id,
            "model_name": self.model_name,
            "status": self.status,
            "quality_score": self.quality_score,
            "queue_wait_ms": self.queue_wait_ms,
            "execution_duration_ms": self.execution_duration_ms,
            "total_latency_ms": self.total_latency_ms,
            "retry_count": self.retry_count,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "recorded_at": self.recorded_at.isoformat(),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskTelemetry:
        """Construct telemetry instance from dictionary representation."""
        rec_at = data.get("recorded_at")
        if isinstance(rec_at, str):
            recorded_at = datetime.fromisoformat(rec_at)
        elif isinstance(rec_at, datetime):
            recorded_at = rec_at
        else:
            recorded_at = datetime.now(UTC)

        return cls(
            task_id=data["task_id"],
            worker_id=data.get("worker_id"),
            node_id=data.get("node_id"),
            model_name=data.get("model_name"),
            status=data.get("status", "completed"),
            quality_score=float(data.get("quality_score", 1.0)),
            queue_wait_ms=float(data.get("queue_wait_ms", 0.0)),
            execution_duration_ms=float(data.get("execution_duration_ms", 0.0)),
            total_latency_ms=float(data.get("total_latency_ms", 0.0)),
            retry_count=int(data.get("retry_count", 0)),
            input_tokens=int(data.get("input_tokens", 0)),
            output_tokens=int(data.get("output_tokens", 0)),
            total_tokens=int(data.get("total_tokens", 0)),
            cost_usd=float(data.get("cost_usd", 0.0)),
            recorded_at=recorded_at,
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class WorkerMetricsAggregate:
    """Aggregated performance metrics for a specific worker."""

    worker_id: str
    total_tasks: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0
    retried_tasks: int = 0
    success_rate: float = 0.0
    avg_quality_score: float = 0.0
    avg_execution_ms: float = 0.0
    p95_execution_ms: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "total_tasks": self.total_tasks,
            "completed_tasks": self.completed_tasks,
            "failed_tasks": self.failed_tasks,
            "retried_tasks": self.retried_tasks,
            "success_rate": self.success_rate,
            "avg_quality_score": self.avg_quality_score,
            "avg_execution_ms": self.avg_execution_ms,
            "p95_execution_ms": self.p95_execution_ms,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_tokens": self.total_tokens,
            "total_cost_usd": self.total_cost_usd,
        }


@dataclass
class WorkloadMetricsAggregate:
    """Aggregated performance and queue metrics across the entire workload."""

    total_tasks: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0
    queued_tasks: int = 0
    in_progress_tasks: int = 0
    review_tasks: int = 0
    avg_queue_wait_ms: float = 0.0
    avg_execution_ms: float = 0.0
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    retry_rate: float = 0.0
    status_breakdown: dict[str, int] = field(default_factory=dict)
    priority_breakdown: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_tasks": self.total_tasks,
            "completed_tasks": self.completed_tasks,
            "failed_tasks": self.failed_tasks,
            "queued_tasks": self.queued_tasks,
            "in_progress_tasks": self.in_progress_tasks,
            "review_tasks": self.review_tasks,
            "avg_queue_wait_ms": self.avg_queue_wait_ms,
            "avg_execution_ms": self.avg_execution_ms,
            "total_cost_usd": self.total_cost_usd,
            "total_tokens": self.total_tokens,
            "retry_rate": self.retry_rate,
            "status_breakdown": dict(self.status_breakdown),
            "priority_breakdown": dict(self.priority_breakdown),
        }
