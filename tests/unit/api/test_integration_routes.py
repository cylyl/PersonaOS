"""Unit tests for PersonaOS Integration Gateway API routes (M1).

Verifies /integration/* endpoints against Pydantic schemas and HTTP semantics.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from personaos.api.routes.integration import router as integration_router


@pytest.fixture
def app() -> FastAPI:
    """FastAPI application with integration router mounted directly and with /api prefix."""
    app = FastAPI(title="Integration Test App")
    app.include_router(integration_router)
    app.include_router(integration_router, prefix="/api")
    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    """TestClient instance."""
    return TestClient(app)


def test_integration_health(client: TestClient):
    """GET /integration/health returns 200 and matches GatewayHealthResponse."""
    resp = client.get("/integration/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["gateway_version"] == "1.0.0"
    assert data["personaos_connected"] is True
    assert data["pab_connected"] is True
    assert "timestamp" in data

    # Test /api/integration/health
    resp_api = client.get("/api/integration/health")
    assert resp_api.status_code == 200
    assert resp_api.json()["gateway_version"] == "1.0.0"


def test_integration_diagnostics(client: TestClient):
    """GET /integration/diagnostics returns 200 with structured diagnostic state."""
    resp = client.get("/integration/diagnostics?target_url=http://127.0.0.1:8000")
    assert resp.status_code == 200
    data = resp.json()
    assert data["code"] == "CONNECTED"
    assert data["status"] == "ok"
    assert data["latency_ms"] >= 0.0
    assert data["upstream_url"] == "http://127.0.0.1:8000"
    assert data["cors_supported"] is True
    assert data["pna_supported"] is True
    assert data["auth_status"] == "authenticated"
    assert data["api_version_match"] is True


def test_integration_diagnostics_cors_blocked_detection(client: TestClient):
    """Diagnostic probe detects secure origin accessing insecure RFC1918 private IP."""
    headers = {"Origin": "https://app.onorca.dev"}
    resp = client.get(
        "/integration/diagnostics?target_url=http://192.168.1.239:8000",
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["code"] == "CORS_BLOCKED"
    assert data["status"] == "degraded"
    assert "Insecure private HTTP URL accessed from secure ADE origin" in data["remediation"]
    assert "localhost:8001" in data["remediation"]


def test_integration_diagnostics_auth_failure(monkeypatch, client: TestClient):
    """Diagnostic probe reports AUTHENTICATION_FAILED when operator token is required."""
    from personaos.config import settings

    monkeypatch.setattr(settings, "operator_token", "secret-test-token-123")

    # Missing token
    resp = client.get("/integration/diagnostics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["code"] == "AUTHENTICATION_FAILED"
    assert data["auth_status"] == "unauthenticated"
    assert "Missing Authorization header" in data["remediation"]

    # Invalid token
    resp_invalid = client.get(
        "/integration/diagnostics",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp_invalid.status_code == 200
    data_invalid = resp_invalid.json()
    assert data_invalid["code"] == "AUTHENTICATION_FAILED"
    assert data_invalid["auth_status"] == "invalid"
    assert "Invalid operator token" in data_invalid["remediation"]

    # Valid token
    resp_valid = client.get(
        "/integration/diagnostics",
        headers={"Authorization": "Bearer secret-test-token-123"},
    )
    assert resp_valid.status_code == 200
    data_valid = resp_valid.json()
    assert data_valid["code"] == "CONNECTED"
    assert data_valid["auth_status"] == "authenticated"


def test_integration_diagnostics_db_failure(app: FastAPI):
    """Diagnostic probe reports SERVICE_UNAVAILABLE when database is down."""
    from unittest.mock import MagicMock

    # Configure a mock session factory that raises on execute
    failing_factory = MagicMock()
    failing_session = MagicMock()

    class FailingSessionContext:
        async def __aenter__(self):
            raise ConnectionRefusedError("PostgreSQL server unreachable")

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    failing_factory.return_value = FailingSessionContext()
    app.state.session_factory = failing_factory

    client = TestClient(app)
    resp = client.get("/integration/diagnostics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["code"] == "SERVICE_UNAVAILABLE"
    assert data["status"] == "error"
    assert "database connection failed" in data["remediation"]

    # Health endpoint also returns down/degraded
    health_resp = client.get("/integration/health")
    assert health_resp.status_code == 200
    health_data = health_resp.json()
    assert health_data["personaos_connected"] is False
    assert health_data["status"] == "down"

    # Clean up app state
    app.state.session_factory = None


def test_integration_workers(client: TestClient):
    """GET /integration/workers returns 200 list."""
    resp = client.get("/integration/workers")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


def test_integration_workers_populated_and_filtered(app: FastAPI):
    """GET /integration/workers returns typed GatewayWorkerSummary models from DB."""
    from unittest.mock import AsyncMock, MagicMock
    from datetime import datetime, UTC
    from personaos.db.models import Worker as WorkerModel

    mock_worker_1 = WorkerModel(
        id="se",
        name="Software Engineer",
        role="software-engineer",
        status="idle",
        current_profile_version=2,
        capabilities=["python", "docker"],
        permissions=["repo:read"],
        updated_at=datetime.now(UTC),
    )
    mock_worker_2 = WorkerModel(
        id="qa",
        name="QA Tester",
        role="qa-engineer",
        status="running",
        current_profile_version=1,
        capabilities=["pytest", "playwright"],
        permissions=["test:run"],
        updated_at=datetime.now(UTC),
    )

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = [mock_worker_1, mock_worker_2]
    mock_result.scalars.return_value = mock_scalars
    mock_session.execute.return_value = mock_result

    class MockSessionContext:
        async def __aenter__(self):
            return mock_session

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    mock_factory = MagicMock(return_value=MockSessionContext())
    app.state.session_factory = mock_factory

    client = TestClient(app)
    resp = client.get("/integration/workers")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert data[0]["id"] == "se"
    assert data[0]["current_profile_version"] == 2
    assert "python" in data[0]["capabilities"]
    assert data[1]["id"] == "qa"
    assert data[1]["status"] == "running"

    # Test filtering by status
    resp_filtered = client.get("/integration/workers?status=idle")
    assert resp_filtered.status_code == 200

    # Clean up app state
    app.state.session_factory = None


def test_integration_runs_lifecycle(client: TestClient):
    """POST /integration/runs and GET /integration/runs."""
    # List runs
    resp = client.get("/integration/runs")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)

    # Create run
    create_payload = {
        "run_id": "run-test-456",
        "objective": "Build gateway contracts",
        "coordinator_handle": "lead-agent",
        "metadata": {"env": "test"},
    }
    create_resp = client.post("/integration/runs", json=create_payload)
    assert create_resp.status_code == 201
    run_data = create_resp.json()
    assert run_data["run_id"] == "run-test-456"
    assert run_data["objective"] == "Build gateway contracts"
    assert run_data["coordinator_handle"] == "lead-agent"
    assert run_data["status"] == "active"


def test_integration_dag_and_ready_waves(client: TestClient):
    """GET /integration/runs/{run_id}/dag and ready-waves."""
    dag_resp = client.get("/integration/runs/run-test-456/dag")
    assert dag_resp.status_code == 200
    dag_data = dag_resp.json()
    assert dag_data["run_id"] == "run-test-456"
    assert "nodes" in dag_data
    assert "edges" in dag_data

    waves_resp = client.get("/integration/runs/run-test-456/ready-waves")
    assert waves_resp.status_code == 200
    waves_data = waves_resp.json()
    assert waves_data["run_id"] == "run-test-456"
    assert "current_wave" in waves_data
    assert "waves" in waves_data


def test_integration_dag_projection_topological_waves(app: FastAPI):
    """Test that DAG projection accurately calculates topological waves and edges."""
    from unittest.mock import AsyncMock, MagicMock
    from datetime import datetime, UTC
    from personaos.db.models import Task as TaskModel

    t1 = TaskModel(
        id="task-root",
        title="Root Task",
        assigned_worker="se",
        status="completed",
        dependencies=[],
        input={"run_id": "run-orch-top"},
        created_at=datetime.now(UTC),
        generation=1,
    )
    t2 = TaskModel(
        id="task-mid",
        title="Mid Tier Task",
        assigned_worker="se",
        status="running",
        dependencies=["task-root"],
        input={"run_id": "run-orch-top"},
        created_at=datetime.now(UTC),
        generation=2,
    )
    t3 = TaskModel(
        id="task-leaf",
        title="Leaf Task",
        assigned_worker="se",
        status="pending",
        dependencies=["task-mid"],
        input={"run_id": "run-orch-top"},
        created_at=datetime.now(UTC),
        generation=1,
    )

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = [t1, t2, t3]
    mock_result.scalars.return_value = mock_scalars
    mock_session.execute.return_value = mock_result

    class MockSessionContext:
        async def __aenter__(self):
            return mock_session

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    app.state.session_factory = MagicMock(return_value=MockSessionContext())
    client = TestClient(app)

    resp = client.get("/integration/runs/run-orch-top/dag")
    assert resp.status_code == 200
    dag = resp.json()

    assert dag["run_id"] == "run-orch-top"
    assert dag["total_tasks"] == 3
    assert dag["completed_tasks"] == 1

    node_map = {n["task_id"]: n for n in dag["nodes"]}
    assert node_map["task-root"]["wave"] == 1
    assert node_map["task-mid"]["wave"] == 2
    assert node_map["task-leaf"]["wave"] == 3

    assert len(dag["edges"]) == 2
    edge_pairs = [(e["from"], e["to"]) for e in dag["edges"]]
    assert ("task-root", "task-mid") in edge_pairs
    assert ("task-mid", "task-leaf") in edge_pairs

    # Also test ready-waves endpoint
    waves_resp = client.get("/integration/runs/run-orch-top/ready-waves")
    assert waves_resp.status_code == 200
    waves = waves_resp.json()
    assert waves["run_id"] == "run-orch-top"
    assert waves["current_wave"] == 2  # Task 2 is running in wave 2

    # Clean up app state
    app.state.session_factory = None


def test_integration_decision_gates(client: TestClient):
    """GET /integration/decision-gates and POST resolve."""
    list_resp = client.get("/integration/decision-gates")
    assert list_resp.status_code == 200
    assert isinstance(list_resp.json(), list)

    # 404 on missing gate
    missing_resp = client.get("/integration/decision-gates/nonexistent-gate")
    assert missing_resp.status_code == 404

    # Resolve gate
    resolve_payload = {
        "decision": "approve",
        "expected_generation": 1,
        "review_notes": "All checks passed",
        "selected_option": "Approve",
    }
    resolve_resp = client.post(
        "/integration/decision-gates/gate-101/resolve", json=resolve_payload
    )
    assert resolve_resp.status_code == 200
    resolve_data = resolve_resp.json()
    assert resolve_data["gate_id"] == "gate-101"
    assert resolve_data["resolved"] is True
    assert resolve_data["status"] == "approved"
    assert resolve_data["new_generation"] == 2


def test_integration_decision_gates_db_backed(app: FastAPI):
    """Database-backed decision gates test generation fencing, approvals, and stale generation conflict."""
    from unittest.mock import AsyncMock, MagicMock
    from personaos.db.models import Task as TaskModel
    from personaos.domain.task import Task

    t_review = TaskModel(
        id="task-gate-001",
        title="Deploy canary build",
        status="review",
        assigned_worker="worker-devops",
        generation=3,
        last_error="Canary latency delta > 5%",
        exhaustion_reason="threshold_exceeded",
        input={"run_id": "run-ci-42"},
        result={"p99_latency_ms": 250},
        retry_count=1,
    )
    t_running = TaskModel(
        id="task-gate-002",
        title="Running non-review task",
        status="running",
        generation=1,
    )

    mock_session = AsyncMock()

    async def mock_execute(stmt):
        mock_result = MagicMock()
        params = list(stmt.compile().params.values()) if hasattr(stmt, "compile") else []
        if "review" in params:
            # Querying review tasks
            mock_result.scalars.return_value.all.return_value = [t_review]
            return mock_result
        elif "task-gate-001" in params:
            mock_result.scalar_one_or_none.return_value = t_review
            return mock_result
        elif "task-gate-002" in params:
            mock_result.scalar_one_or_none.return_value = t_running
            return mock_result
        else:
            mock_result.scalars.return_value.all.return_value = []
            mock_result.scalar_one_or_none.return_value = None
            return mock_result

    mock_session.execute = AsyncMock(side_effect=mock_execute)
    mock_session.commit = AsyncMock()

    class MockSessionContext:
        async def __aenter__(self):
            return mock_session

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    app.state.session_factory = MagicMock(return_value=MockSessionContext())
    client = TestClient(app)

    # 1. List decision gates
    gates_resp = client.get("/integration/decision-gates")
    assert gates_resp.status_code == 200
    gates = gates_resp.json()
    assert len(gates) == 1
    assert gates[0]["gate_id"] == "gate-task-gate-001"
    assert gates[0]["task_id"] == "task-gate-001"
    assert gates[0]["run_id"] == "run-ci-42"
    assert gates[0]["execution_generation"] == 3
    assert gates[0]["status"] == "pending"
    assert "threshold_exceeded" in gates[0]["question"]

    # 2. Get specific gate detail
    gate_detail_resp = client.get("/integration/decision-gates/gate-task-gate-001")
    assert gate_detail_resp.status_code == 200
    gate_detail = gate_detail_resp.json()
    assert gate_detail["gate_id"] == "gate-task-gate-001"
    assert gate_detail["review_context"]["last_error"] == "Canary latency delta > 5%"

    # 3. Non-existent gate 404
    missing_resp = client.get("/integration/decision-gates/gate-missing-999")
    assert missing_resp.status_code == 404

    # 4. Resolve with STALE generation mismatch -> HTTP 409
    stale_payload = {
        "decision": "approve",
        "expected_generation": 2,  # Actual is 3
        "review_notes": "Attempting stale approve",
    }
    conflict_resp = client.post(
        "/integration/decision-gates/gate-task-gate-001/resolve",
        json=stale_payload,
    )
    assert conflict_resp.status_code == 409
    assert "STALE_GENERATION_CONFLICT" in conflict_resp.json()["detail"]

    # 5. Non-review status task -> HTTP 409
    running_payload = {
        "decision": "approve",
        "expected_generation": 1,
        "review_notes": "Trying to resolve running task",
    }
    running_resp = client.post(
        "/integration/decision-gates/gate-task-gate-002/resolve",
        json=running_payload,
    )
    assert running_resp.status_code == 409
    assert "expected 'review'" in running_resp.json()["detail"]

    # 6. Valid approval with matching expected_generation
    from unittest.mock import patch
    with patch("personaos.api.routes.integration.TaskReviewService") as MockReviewSvcClass:
        mock_svc = MockReviewSvcClass.return_value
        mock_updated = MagicMock()
        mock_updated.id = "task-gate-001"
        mock_updated.generation = 4
        mock_svc.approve = AsyncMock(return_value=mock_updated)
        mock_svc.reject = AsyncMock(return_value=mock_updated)

        valid_payload = {
            "decision": "approve",
            "expected_generation": 3,
            "review_notes": "Operator sign-off verified",
            "selected_option": "approve",
        }
        res_resp = client.post(
            "/integration/decision-gates/gate-task-gate-001/resolve",
            json=valid_payload,
        )
        assert res_resp.status_code == 200
        res_data = res_resp.json()
        assert res_data["gate_id"] == "gate-task-gate-001"
        assert res_data["task_id"] == "task-gate-001"
        assert res_data["resolved"] is True
        assert res_data["status"] == "approved"
        assert res_data["new_generation"] == 4
        mock_svc.approve.assert_called_once_with(
            task_id="task-gate-001",
            expected_generation=3,
            operator_id="orca:operator",
            reason="Operator sign-off verified",
        )

        # 7. Valid rejection
        reject_payload = {
            "decision": "reject",
            "expected_generation": 3,
            "review_notes": "Security finding unaddressed",
            "selected_option": "reject",
        }
        rej_resp = client.post(
            "/integration/decision-gates/gate-task-gate-001/resolve",
            json=reject_payload,
        )
        assert rej_resp.status_code == 200
        rej_data = rej_resp.json()
        assert rej_data["gate_id"] == "gate-task-gate-001"
        assert rej_data["status"] == "rejected"
        mock_svc.reject.assert_called_once_with(
            task_id="task-gate-001",
            expected_generation=3,
            operator_id="orca:operator",
            reason="Security finding unaddressed",
        )

    # Clean up app state
    app.state.session_factory = None


def test_integration_events_sse_heartbeat(client: TestClient):
    """GET /integration/events streams keepalive heartbeats with text/event-stream headers."""
    with client.stream("GET", "/integration/events?heartbeat_interval=0.05&max_events=2") as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        assert resp.headers["cache-control"] == "no-cache"
        assert resp.headers["connection"] == "keep-alive"

        lines = [line for line in resp.iter_lines() if line]
        assert any("heartbeat" in l for l in lines)


def test_integration_events_sse_publish_and_receive(client: TestClient):
    """Events published to gateway_event_bus are delivered to active SSE streams."""
    from personaos.integrations.common.event_bus import gateway_event_bus

    gateway_event_bus.clear()

    # Pre-publish an event to verify buffer replay or live reception
    pub_evt = gateway_event_bus.publish(
        event_type="task_status_changed",
        payload={"task_id": "task-sse-1", "status": "completed", "run_id": "run-sse-live"},
        event_id="evt-sse-001",
    )

    with client.stream("GET", "/integration/events?heartbeat_interval=0.1&max_events=2") as resp:
        assert resp.status_code == 200
        lines = [line for line in resp.iter_lines() if line]
        assert any("connected" in l or "task_status_changed" in l for l in lines)

    # Also test POST /integration/events/publish endpoint
    post_payload = {
        "event_id": "evt-post-002",
        "event_type": "gate_created",
        "payload": {"gate_id": "gate-test-99", "status": "pending"},
    }
    pub_resp = client.post("/integration/events/publish", json=post_payload)
    assert pub_resp.status_code == 200
    pub_data = pub_resp.json()
    assert pub_data["event_id"] == "evt-post-002"
    assert pub_data["event_type"] == "gate_created"


def test_integration_events_sse_reconnection_last_event_id(client: TestClient):
    """Clients reconnecting with Last-Event-ID receive missed events in sequence."""
    from personaos.integrations.common.event_bus import gateway_event_bus

    gateway_event_bus.clear()

    e1 = gateway_event_bus.publish("task_status_changed", {"task_id": "t-1"}, event_id="evt-101")
    e2 = gateway_event_bus.publish("ready_wave_advanced", {"wave": 2}, event_id="evt-102")
    e3 = gateway_event_bus.publish("gate_resolved", {"gate_id": "g-1"}, event_id="evt-103")

    # Connect with Last-Event-ID header set to evt-101; expect evt-102 and evt-103
    headers = {"Last-Event-ID": "evt-101"}
    with client.stream("GET", "/integration/events?heartbeat_interval=0.1&max_events=2", headers=headers) as resp:
        assert resp.status_code == 200
        received_ids = []
        for line in resp.iter_lines():
            if line.startswith("id:"):
                received_ids.append(line.replace("id:", "").strip())

        assert "evt-102" in received_ids
        assert "evt-103" in received_ids
        assert "evt-101" not in received_ids


def test_integration_events_sse_reconnection_query_param(client: TestClient):
    """Clients reconnecting with ?last_event_id query param receive missed events."""
    from personaos.integrations.common.event_bus import gateway_event_bus

    gateway_event_bus.clear()

    gateway_event_bus.publish("task_status_changed", {"task_id": "t-1"}, event_id="evt-qp-1")
    gateway_event_bus.publish("ready_wave_advanced", {"wave": 2}, event_id="evt-qp-2")

    with client.stream("GET", "/integration/events?last_event_id=evt-qp-1&heartbeat_interval=0.1&max_events=1") as resp:
        assert resp.status_code == 200
        received_ids = []
        for line in resp.iter_lines():
            if line.startswith("id:"):
                received_ids.append(line.replace("id:", "").strip())
        assert "evt-qp-2" in received_ids
        assert "evt-qp-1" not in received_ids


def test_integration_events_sse_run_id_filtering(client: TestClient):
    """SSE stream filters out events intended for other coordination runs."""
    from personaos.integrations.common.event_bus import gateway_event_bus

    gateway_event_bus.clear()

    gateway_event_bus.publish("task_status_changed", {"run_id": "run-alpha"}, event_id="evt-run-1")
    gateway_event_bus.publish("task_status_changed", {"run_id": "run-beta"}, event_id="evt-run-2")

    # Subscribe filtered to run-alpha
    headers = {"Last-Event-ID": "evt-run-0"}  # trigger search from start
    with client.stream("GET", "/integration/events?run_id=run-alpha&heartbeat_interval=0.1&max_events=1") as resp:
        assert resp.status_code == 200


def test_integration_events_sse_db_fallback_replay(app: FastAPI):
    """When last_event_id is not in memory ring buffer, replay falls back to DB TaskEvents."""
    from unittest.mock import AsyncMock, MagicMock
    from datetime import datetime, UTC, timedelta
    from personaos.db.models import TaskEvent as TaskEventModel
    from personaos.integrations.common.event_bus import gateway_event_bus

    gateway_event_bus.clear()  # Empty memory buffer

    now = datetime.now(UTC)
    t_event_ref = TaskEventModel(
        id="dbe-ref-001",
        task_id="task-db-1",
        type="task.created",
        actor_id="test",
        payload={"step": "created"},
        created_at=now - timedelta(seconds=10),
    )
    t_event_next = TaskEventModel(
        id="dbe-next-002",
        task_id="task-db-1",
        type="task.completed",
        actor_id="test",
        payload={"step": "completed"},
        created_at=now - timedelta(seconds=5),
    )

    mock_session = AsyncMock()

    async def mock_execute(stmt):
        mock_res = MagicMock()
        # If querying for reference event
        params = list(stmt.compile().params.values()) if hasattr(stmt, "compile") else []
        if "dbe-ref-001" in params:
            mock_res.scalar_one_or_none.return_value = t_event_ref
        else:
            mock_res.scalars.return_value.all.return_value = [t_event_ref, t_event_next]
        return mock_res

    mock_session.execute = AsyncMock(side_effect=mock_execute)

    class MockSessionContext:
        async def __aenter__(self):
            return mock_session

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    app.state.session_factory = MagicMock(return_value=MockSessionContext())
    client = TestClient(app)

    headers = {"Last-Event-ID": "dbe-ref-001"}
    with client.stream("GET", "/integration/events?heartbeat_interval=0.1&max_events=1", headers=headers) as resp:
        assert resp.status_code == 200
        received_ids = []
        for line in resp.iter_lines():
            if line.startswith("id:"):
                received_ids.append(line.replace("id:", "").strip())
        assert "dbe-next-002" in received_ids

    app.state.session_factory = None


@pytest.mark.asyncio
async def test_emit_task_event_publishes_to_gateway_bus():
    """emit_task_event in personaos.events.store publishes to gateway_event_bus."""
    from unittest.mock import AsyncMock
    from personaos.events.store import emit_task_event
    from personaos.integrations.common.event_bus import gateway_event_bus

    gateway_event_bus.clear()

    mock_session = AsyncMock()
    await emit_task_event(
        mock_session,
        task_id="task-store-99",
        event_type="approval.requested",
        actor_id="test-actor",
        payload={"reason": "security_check"},
    )

    history = gateway_event_bus.get_history(limit=5)
    assert len(history) == 1
    assert history[0].event_type == "gate_created"
    assert history[0].payload["task_id"] == "task-store-99"
    assert history[0].payload["actor_id"] == "test-actor"
    assert history[0].payload["reason"] == "security_check"


def test_integration_panel_ui(client: TestClient):
    """GET /integration/panel returns 200 with HTML content."""
    resp = client.get("/integration/panel")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "PersonaOS" in resp.text
    assert "Orca ADE" in resp.text


def test_cors_opaque_origin_sandboxed_iframe():
    """Ensure sandboxed iframe requests (Origin: null) receive valid CORS and PNA headers without credentials conflict."""
    from personaos.api.app import create_app

    full_app = create_app()
    with TestClient(full_app) as full_client:
        # Preflight
        resp = full_client.options(
            "/integration/diagnostics",
            headers={
                "Origin": "null",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
                "Access-Control-Request-Private-Network": "true",
            },
        )
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == "*"
        assert resp.headers.get("access-control-allow-credentials") is None
        assert resp.headers.get("access-control-allow-private-network") == "true"

        # Actual request
        resp_get = full_client.get("/integration/diagnostics", headers={"Origin": "null"})
        assert resp_get.status_code == 200
        assert resp_get.headers.get("access-control-allow-origin") == "*"
        assert resp_get.headers.get("access-control-allow-credentials") is None






