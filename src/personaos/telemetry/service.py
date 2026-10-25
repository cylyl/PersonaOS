"""TelemetryService — coordinates telemetry collection, evaluation, and aggregation.

Per ADR 0029 and docs/specs/task-telemetry-quality-latency-cost.md:
Tracks task execution latency, token counts, costs, and quality scores.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from personaos.db.models import Task as TaskModel
from personaos.domain.task import Task
from personaos.telemetry.models import (
    TaskTelemetry,
    WorkerMetricsAggregate,
    WorkloadMetricsAggregate,
)
from personaos.telemetry.pricing import ModelPricingRegistry
from personaos.telemetry.quality import QualityEvaluator

logger = logging.getLogger(__name__)


class TelemetryService:
    """Service for measuring, recording, and aggregating execution telemetry."""

    def __init__(
        self,
        pricing_registry: ModelPricingRegistry | None = None,
        quality_evaluator: QualityEvaluator | None = None,
    ) -> None:
        self.pricing = pricing_registry or ModelPricingRegistry()
        self.evaluator = quality_evaluator or QualityEvaluator()

    def record_telemetry(
        self,
        task: Task | TaskModel,
        output: dict[str, Any] | None = None,
        model_name: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        quality_score: float | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskTelemetry:
        """Construct a standardized TaskTelemetry record for a task."""
        created = getattr(task, "created_at", None) or datetime.now(UTC)
        start = started_at or getattr(task, "started_at", None) or created
        finish = completed_at or getattr(task, "completed_at", None) or datetime.now(UTC)

        # Calculate durations in milliseconds
        queue_wait_ms = max(0.0, (start - created).total_seconds() * 1000.0)
        execution_duration_ms = max(0.0, (finish - start).total_seconds() * 1000.0)
        total_latency_ms = max(0.0, (finish - created).total_seconds() * 1000.0)

        # Quality evaluation
        retry_count = getattr(task, "retry_count", 0)
        status = getattr(task, "status", "completed")
        last_error = getattr(task, "last_error", None)

        if quality_score is None:
            q_score = self.evaluator.evaluate(
                status=status,
                output=output,
                error=last_error,
                retry_count=retry_count,
            )
        else:
            q_score = max(0.0, min(1.0, float(quality_score)))

        # Token & Cost calculation
        total_tokens = input_tokens + output_tokens
        cost = self.pricing.calculate_cost(
            model_name=model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

        return TaskTelemetry(
            task_id=task.id,
            worker_id=getattr(task, "assigned_worker", None),
            node_id=getattr(task, "assigned_node", None),
            model_name=model_name,
            status=status,
            quality_score=q_score,
            queue_wait_ms=round(queue_wait_ms, 2),
            execution_duration_ms=round(execution_duration_ms, 2),
            total_latency_ms=round(total_latency_ms, 2),
            retry_count=retry_count,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cost_usd=cost,
            recorded_at=datetime.now(UTC),
            metadata=dict(metadata or {}),
        )

    def attach_telemetry_to_result(
        self,
        result_payload: dict[str, Any] | None,
        telemetry: TaskTelemetry,
    ) -> dict[str, Any]:
        """Attach telemetry data to the task result dictionary for persistence."""
        payload = dict(result_payload or {})
        payload["telemetry"] = telemetry.to_dict()
        return payload

    async def get_task_telemetry(
        self,
        session: AsyncSession,
        task_id: str,
    ) -> TaskTelemetry | None:
        """Retrieve telemetry for a task from database."""
        stmt = select(TaskModel).where(TaskModel.id == task_id)
        res = await session.execute(stmt)
        task_row = res.scalars().first()
        if not task_row:
            return None

        # Check if telemetry is already serialized in result JSON
        if task_row.result and isinstance(task_row.result, dict):
            telemetry_data = task_row.result.get("telemetry")
            if isinstance(telemetry_data, dict):
                return TaskTelemetry.from_dict(telemetry_data)

        # Otherwise synthesize from row attributes
        return self.record_telemetry(task_row, output=task_row.result)

    async def aggregate_worker_metrics(
        self,
        session: AsyncSession,
        worker_id: str,
        window_days: int | None = None,
    ) -> WorkerMetricsAggregate:
        """Aggregate performance and cost metrics for a worker."""
        stmt = select(TaskModel).where(TaskModel.assigned_worker == worker_id)
        if window_days is not None:
            cutoff = datetime.now(UTC) - timedelta(days=window_days)
            stmt = stmt.where(TaskModel.created_at >= cutoff)

        res = await session.execute(stmt)
        tasks = list(res.scalars().all())

        total = len(tasks)
        if total == 0:
            return WorkerMetricsAggregate(worker_id=worker_id)

        completed = 0
        failed = 0
        retried = 0
        quality_scores: list[float] = []
        execution_durations: list[float] = []
        total_input_toks = 0
        total_output_toks = 0
        total_cost = 0.0

        for t in tasks:
            if t.status == "completed":
                completed += 1
            elif t.status in ("failed", "canceled"):
                failed += 1

            if t.retry_count > 0:
                retried += 1

            # Extract or synthesize telemetry
            tel: TaskTelemetry
            if t.result and isinstance(t.result, dict) and "telemetry" in t.result:
                tel = TaskTelemetry.from_dict(t.result["telemetry"])
            else:
                tel = self.record_telemetry(t, output=t.result)

            quality_scores.append(tel.quality_score)
            if tel.execution_duration_ms > 0:
                execution_durations.append(tel.execution_duration_ms)
            total_input_toks += tel.input_tokens
            total_output_toks += tel.output_tokens
            total_cost += tel.cost_usd

        success_rate = round(completed / total, 4) if total > 0 else 0.0
        avg_quality = round(sum(quality_scores) / len(quality_scores), 4) if quality_scores else 0.0
        avg_exec = (
            round(sum(execution_durations) / len(execution_durations), 2)
            if execution_durations
            else 0.0
        )

        # p95 execution duration
        p95_exec = 0.0
        if execution_durations:
            sorted_durations = sorted(execution_durations)
            p95_idx = int(0.95 * len(sorted_durations))
            p95_exec = round(sorted_durations[min(p95_idx, len(sorted_durations) - 1)], 2)

        return WorkerMetricsAggregate(
            worker_id=worker_id,
            total_tasks=total,
            completed_tasks=completed,
            failed_tasks=failed,
            retried_tasks=retried,
            success_rate=success_rate,
            avg_quality_score=avg_quality,
            avg_execution_ms=avg_exec,
            p95_execution_ms=p95_exec,
            total_input_tokens=total_input_toks,
            total_output_tokens=total_output_toks,
            total_tokens=total_input_toks + total_output_toks,
            total_cost_usd=round(total_cost, 4),
        )

    async def aggregate_workload_metrics(
        self,
        session: AsyncSession,
        window_days: int | None = None,
    ) -> WorkloadMetricsAggregate:
        """Aggregate workload throughput, latency, and status breakdown across all tasks."""
        base_filter = []
        if window_days is not None:
            cutoff = datetime.now(UTC) - timedelta(days=window_days)
            base_filter.append(TaskModel.created_at >= cutoff)

        # Group by status
        status_stmt = (
            select(TaskModel.status, func.count(TaskModel.id))
            .where(*base_filter)
            .group_by(TaskModel.status)
        )
        status_res = await session.execute(status_stmt)
        status_breakdown = {row[0]: row[1] for row in status_res.all()}

        # Group by priority
        priority_stmt = (
            select(TaskModel.priority, func.count(TaskModel.id))
            .where(*base_filter)
            .group_by(TaskModel.priority)
        )
        prio_res = await session.execute(priority_stmt)
        priority_breakdown = {row[0]: row[1] for row in prio_res.all()}

        # Load completed/failed tasks for latency and token aggregation
        tasks_stmt = select(TaskModel).where(*base_filter)
        tasks_res = await session.execute(tasks_stmt)
        all_tasks = list(tasks_res.scalars().all())

        total = max(len(all_tasks), sum(status_breakdown.values()))
        if total == 0:
            return WorkloadMetricsAggregate()

        completed = status_breakdown.get("completed", 0)
        failed = status_breakdown.get("failed", 0)
        queued = status_breakdown.get("queued", 0)
        in_progress = status_breakdown.get("in_progress", 0)
        review = status_breakdown.get("review", 0)

        retried_count = sum(1 for t in all_tasks if t.retry_count > 0)
        retry_rate = round(retried_count / total, 4) if total > 0 else 0.0

        queue_waits: list[float] = []
        exec_durations: list[float] = []
        total_tokens = 0
        total_cost = 0.0

        for t in all_tasks:
            tel: TaskTelemetry
            if t.result and isinstance(t.result, dict) and "telemetry" in t.result:
                tel = TaskTelemetry.from_dict(t.result["telemetry"])
            else:
                tel = self.record_telemetry(t, output=t.result)

            if tel.queue_wait_ms > 0:
                queue_waits.append(tel.queue_wait_ms)
            if tel.execution_duration_ms > 0:
                exec_durations.append(tel.execution_duration_ms)

            total_tokens += tel.total_tokens
            total_cost += tel.cost_usd

        avg_queue_wait = round(sum(queue_waits) / len(queue_waits), 2) if queue_waits else 0.0
        avg_exec = round(sum(exec_durations) / len(exec_durations), 2) if exec_durations else 0.0

        return WorkloadMetricsAggregate(
            total_tasks=total,
            completed_tasks=completed,
            failed_tasks=failed,
            queued_tasks=queued,
            in_progress_tasks=in_progress,
            review_tasks=review,
            avg_queue_wait_ms=avg_queue_wait,
            avg_execution_ms=avg_exec,
            total_cost_usd=round(total_cost, 4),
            total_tokens=total_tokens,
            retry_rate=retry_rate,
            status_breakdown=status_breakdown,
            priority_breakdown=priority_breakdown,
        )
