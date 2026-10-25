"""Unit tests for Task Execution Telemetry (Vikunja #112, ADR 0029)."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from personaos.domain.task import Task
from personaos.telemetry.models import (
    TaskTelemetry,
)
from personaos.telemetry.pricing import ModelPricingRegistry
from personaos.telemetry.quality import QualityEvaluator
from personaos.telemetry.service import TelemetryService


def test_model_pricing_registry() -> None:
    registry = ModelPricingRegistry()

    # Exact match
    price_gpt4o = registry.get_price("gpt-4o")
    assert price_gpt4o.input_per_million == 2.50
    assert price_gpt4o.output_per_million == 10.00

    # Prefix match
    price_sonnet_dated = registry.get_price("claude-3-5-sonnet-20241022")
    assert price_sonnet_dated.input_per_million == 3.00
    assert price_sonnet_dated.output_per_million == 15.00

    # Fallback match
    price_unknown = registry.get_price("my-custom-model")
    assert price_unknown.input_per_million == 1.00

    # Cost calculation: 10,000 input tokens, 2,000 output tokens on gpt-4o
    # input: 10,000 / 1,000,000 * 2.50 = 0.025
    # output: 2,000 / 1,000,000 * 10.00 = 0.020
    # total = 0.045
    cost = registry.calculate_cost("gpt-4o", input_tokens=10_000, output_tokens=2_000)
    assert cost == 0.045

    # Negative tokens error
    with pytest.raises(ValueError, match="negative"):
        registry.calculate_cost("gpt-4o", input_tokens=-10, output_tokens=100)


def test_quality_evaluator() -> None:
    evaluator = QualityEvaluator(retry_penalty=0.1, min_completed_score=0.2)

    # 1. Clean completion
    score_clean = evaluator.evaluate(status="completed", output={"summary": "done"})
    assert score_clean == 1.0

    # 2. Completed after 2 retries
    score_retried = evaluator.evaluate(
        status="completed", output={"summary": "done"}, retry_count=2
    )
    assert score_retried == 0.8

    # 3. Completed with empty output
    score_empty_out = evaluator.evaluate(status="completed", output=None, retry_count=0)
    assert score_empty_out == 0.85

    # 4. Excessive retries clamped to floor
    score_many_retries = evaluator.evaluate(
        status="completed", output={"summary": "done"}, retry_count=10
    )
    assert score_many_retries == 0.2

    # 5. Failed task
    score_failed = evaluator.evaluate(status="failed", error="Fatal error", retry_count=3)
    assert score_failed == 0.0


def test_telemetry_model_roundtrip() -> None:
    now = datetime.now(UTC)
    tel = TaskTelemetry(
        task_id="task-123",
        worker_id="worker-qa",
        node_id="node-1",
        model_name="gpt-4o",
        status="completed",
        quality_score=0.9,
        queue_wait_ms=150.5,
        execution_duration_ms=4500.25,
        total_latency_ms=4650.75,
        retry_count=1,
        input_tokens=1500,
        output_tokens=300,
        total_tokens=1800,
        cost_usd=0.00675,
        recorded_at=now,
    )

    data = tel.to_dict()
    assert data["task_id"] == "task-123"
    assert data["total_tokens"] == 1800
    assert data["cost_usd"] == 0.00675

    rebuilt = TaskTelemetry.from_dict(data)
    assert rebuilt.task_id == tel.task_id
    assert rebuilt.quality_score == 0.9
    assert rebuilt.queue_wait_ms == 150.5


def test_telemetry_service_record_and_attach() -> None:
    created = datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)
    started = created + timedelta(seconds=2)
    completed = started + timedelta(seconds=10)

    task = Task(
        id="t-1",
        title="Analyze Repo",
        objective="Analyze code",
        input={"repo": "yabby"},
        profile_version=1,
        status="completed",
        priority="high",
        assigned_worker="worker-dev",
        assigned_node="node-alpha",
        retry_count=0,
        created_at=created,
        started_at=started,
        completed_at=completed,
    )

    service = TelemetryService()
    telemetry = service.record_telemetry(
        task=task,
        output={"status": "ok"},
        model_name="claude-3-5-sonnet",
        input_tokens=20_000,
        output_tokens=5_000,
    )

    assert telemetry.task_id == "t-1"
    assert telemetry.worker_id == "worker-dev"
    assert telemetry.queue_wait_ms == 2000.0
    assert telemetry.execution_duration_ms == 10000.0
    assert telemetry.total_latency_ms == 12000.0
    assert telemetry.total_tokens == 25_000
    # 20k input ($3/M) = $0.06, 5k output ($15/M) = $0.075 -> total $0.135
    assert telemetry.cost_usd == 0.135
    assert telemetry.quality_score == 1.0

    # Attach to result dict
    res_dict = service.attach_telemetry_to_result({"report": "passed"}, telemetry)
    assert "telemetry" in res_dict
    assert res_dict["telemetry"]["cost_usd"] == 0.135


@pytest.mark.asyncio
async def test_worker_and_workload_aggregation() -> None:
    service = TelemetryService()

    # Mock Task rows
    class MockRow:
        def __init__(
            self,
            tid: str,
            worker: str,
            status: str,
            priority: str,
            created: datetime,
            started: datetime,
            completed: datetime,
            retry: int,
            tokens_in: int,
            tokens_out: int,
        ):
            self.id = tid
            self.assigned_worker = worker
            self.assigned_node = "n1"
            self.status = status
            self.priority = priority
            self.created_at = created
            self.started_at = started
            self.completed_at = completed
            self.retry_count = retry
            self.last_error = None
            price = service.pricing.calculate_cost("gpt-4o", tokens_in, tokens_out)
            self.result = {
                "telemetry": {
                    "task_id": tid,
                    "worker_id": worker,
                    "status": status,
                    "quality_score": 1.0 if status == "completed" else 0.0,
                    "queue_wait_ms": 1000.0,
                    "execution_duration_ms": 5000.0,
                    "total_latency_ms": 6000.0,
                    "retry_count": retry,
                    "input_tokens": tokens_in,
                    "output_tokens": tokens_out,
                    "total_tokens": tokens_in + tokens_out,
                    "cost_usd": price,
                }
            }

    t0 = datetime(2026, 10, 24, 10, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(seconds=1)
    t2 = t1 + timedelta(seconds=5)

    rows = [
        MockRow("t-1", "w-1", "completed", "high", t0, t1, t2, 0, 10_000, 2_000),
        MockRow("t-2", "w-1", "completed", "medium", t0, t1, t2, 1, 20_000, 4_000),
        MockRow("t-3", "w-1", "failed", "low", t0, t1, t2, 2, 5_000, 500),
    ]

    mock_session = AsyncMock()
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = rows
    mock_res = MagicMock()
    mock_res.scalars.return_value = mock_scalars
    mock_session.execute.return_value = mock_res

    # Worker aggregate
    worker_agg = await service.aggregate_worker_metrics(mock_session, worker_id="w-1")
    assert worker_agg.total_tasks == 3
    assert worker_agg.completed_tasks == 2
    assert worker_agg.failed_tasks == 1
    assert worker_agg.retried_tasks == 2
    assert worker_agg.success_rate == 0.6667
    assert worker_agg.total_input_tokens == 35_000
    assert worker_agg.total_output_tokens == 6_500
    assert worker_agg.total_cost_usd > 0.0

    # Workload aggregate mock
    mock_status_res = MagicMock()
    mock_status_res.all.return_value = [("completed", 2), ("failed", 1)]
    mock_prio_res = MagicMock()
    mock_prio_res.all.return_value = [("high", 1), ("medium", 1), ("low", 1)]

    # Mock session side_effect for multiple queries
    mock_session.execute.side_effect = [mock_status_res, mock_prio_res, mock_res]

    workload_agg = await service.aggregate_workload_metrics(mock_session)
    assert workload_agg.total_tasks == 3
    assert workload_agg.completed_tasks == 2
    assert workload_agg.failed_tasks == 1
    assert workload_agg.retry_rate == 0.6667
    assert workload_agg.status_breakdown == {"completed": 2, "failed": 1}
    assert workload_agg.priority_breakdown == {"high": 1, "medium": 1, "low": 1}
