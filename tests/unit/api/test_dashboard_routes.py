"""Unit tests for Worker and Workload Dashboard routes (Vikunja #113, ADR 0030)."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from personaos.api.deps import get_session
from personaos.api.routes.dashboard import router as dashboard_router
from personaos.db.models import Task as TaskModel
from personaos.db.models import Worker as WorkerModel


@pytest.fixture
def mock_session() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def test_app(mock_session: AsyncMock) -> FastAPI:
    app = FastAPI()
    app.include_router(dashboard_router)

    async def _override_session():
        yield mock_session

    app.dependency_overrides[get_session] = _override_session
    return app


def test_dashboard_ui(test_app: FastAPI) -> None:
    client = TestClient(test_app)
    response = client.get("/dashboard/ui")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "PersonaOS Dashboard" in response.text
    assert "Workload Overview" in response.text


def test_dashboard_workload_endpoint(test_app: FastAPI, mock_session: AsyncMock) -> None:
    # Setup mock DB responses
    mock_status_res = MagicMock()
    mock_status_res.all.return_value = [("completed", 5), ("queued", 2), ("failed", 1)]
    mock_prio_res = MagicMock()
    mock_prio_res.all.return_value = [("high", 3), ("medium", 5)]

    # Mock empty scalar list for tasks
    mock_tasks_res = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = []
    mock_tasks_res.scalars.return_value = mock_scalars

    mock_session.execute.side_effect = [mock_status_res, mock_prio_res, mock_tasks_res]

    client = TestClient(test_app)

    response = client.get("/dashboard/workload?window_days=7")
    assert response.status_code == 200
    data = response.json()
    assert data["queued_tasks"] == 2
    assert data["completed_tasks"] == 5
    assert data["failed_tasks"] == 1
    assert data["status_breakdown"] == {"completed": 5, "queued": 2, "failed": 1}
    assert data["priority_breakdown"] == {"high": 3, "medium": 5}


def test_dashboard_workers_endpoint(test_app: FastAPI, mock_session: AsyncMock) -> None:
    w1 = WorkerModel(
        id="worker-qa",
        name="QA Engineer",
        role="tester",
        status="active",
        current_profile_version=2,
        created_at=datetime.now(UTC),
    )

    # 1. workers query
    mock_workers_res = MagicMock()
    mock_w_scalars = MagicMock()
    mock_w_scalars.all.return_value = [w1]
    mock_workers_res.scalars.return_value = mock_w_scalars

    # 2. active tasks count query
    mock_active_res = MagicMock()
    mock_active_res.all.return_value = [("worker-qa", 1)]

    # 3. worker tasks query for metrics
    mock_worker_tasks_res = MagicMock()
    mock_wt_scalars = MagicMock()
    mock_wt_scalars.all.return_value = []
    mock_worker_tasks_res.scalars.return_value = mock_wt_scalars

    mock_session.execute.side_effect = [
        mock_workers_res,
        mock_active_res,
        mock_worker_tasks_res,
    ]

    client = TestClient(test_app)

    response = client.get("/dashboard/workers")
    assert response.status_code == 200
    data = response.json()
    assert data["total_workers"] == 1
    assert data["workers"][0]["worker_id"] == "worker-qa"
    assert data["workers"][0]["active_tasks_count"] == 1
    assert "metrics" in data["workers"][0]


def test_dashboard_worker_detail_not_found(test_app: FastAPI, mock_session: AsyncMock) -> None:
    mock_res = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.first.return_value = None
    mock_res.scalars.return_value = mock_scalars
    mock_session.execute.return_value = mock_res

    client = TestClient(test_app)

    response = client.get("/dashboard/workers/unknown-worker")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_dashboard_task_telemetry_detail(test_app: FastAPI, mock_session: AsyncMock) -> None:
    now = datetime.now(UTC)
    task_row = TaskModel(
        id="task-99",
        title="Benchmark",
        objective="Performance testing",
        input={},
        status="completed",
        priority="high",
        assigned_worker="worker-perf",
        assigned_node="node-1",
        retry_count=0,
        result={
            "telemetry": {
                "task_id": "task-99",
                "worker_id": "worker-perf",
                "node_id": "node-1",
                "model_name": "gpt-4o",
                "status": "completed",
                "quality_score": 1.0,
                "queue_wait_ms": 250.0,
                "execution_duration_ms": 3200.0,
                "total_latency_ms": 3450.0,
                "retry_count": 0,
                "input_tokens": 1200,
                "output_tokens": 400,
                "total_tokens": 1600,
                "cost_usd": 0.007,
                "recorded_at": now.isoformat(),
            }
        },
        created_at=now,
        started_at=now,
        completed_at=now,
    )

    mock_res = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.first.return_value = task_row
    mock_res.scalars.return_value = mock_scalars
    mock_session.execute.return_value = mock_res

    client = TestClient(test_app)

    response = client.get("/dashboard/tasks/task-99/telemetry")
    assert response.status_code == 200
    data = response.json()
    assert data["task_id"] == "task-99"
    assert data["cost_usd"] == 0.007
    assert data["total_tokens"] == 1600
    assert data["quality_score"] == 1.0
