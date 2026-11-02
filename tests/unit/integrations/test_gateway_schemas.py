"""Unit tests for PersonaOS Integration Gateway Pydantic schemas (M1).

Verifies validation, serialization, deserialization, generation fencing,
edge alias handling, and OpenAPI generation.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from personaos.api.integration_openapi import (
    dump_integration_openapi_schema,
    get_integration_openapi_schema,
)
from personaos.integrations.common.gateway_schemas import (
    ConnectionDiagnosticCode,
    GatewayDagEdge,
    GatewayDagNode,
    GatewayDagProjection,
    GatewayDecisionGate,
    GatewayDiagnosticsResponse,
    GatewayEventMessage,
    GatewayGateResolveRequest,
    GatewayGateResolveResponse,
    GatewayHealthResponse,
    GatewayReadyTask,
    GatewayReadyWave,
    GatewayReadyWavesResponse,
    GatewayRunCreateRequest,
    GatewayRunSummary,
    GatewayWorkerSummary,
)


def test_connection_diagnostic_code_enum():
    """Verify diagnostic code enum values match specification."""
    assert ConnectionDiagnosticCode.CONNECTED == "CONNECTED"
    assert ConnectionDiagnosticCode.NETWORK_UNREACHABLE == "NETWORK_UNREACHABLE"
    assert ConnectionDiagnosticCode.CORS_BLOCKED == "CORS_BLOCKED"
    assert ConnectionDiagnosticCode.AUTHENTICATION_FAILED == "AUTHENTICATION_FAILED"
    assert ConnectionDiagnosticCode.ENDPOINT_NOT_FOUND == "ENDPOINT_NOT_FOUND"
    assert ConnectionDiagnosticCode.API_VERSION_MISMATCH == "API_VERSION_MISMATCH"
    assert ConnectionDiagnosticCode.INVALID_RESPONSE == "INVALID_RESPONSE"
    assert ConnectionDiagnosticCode.SERVICE_UNAVAILABLE == "SERVICE_UNAVAILABLE"


def test_gateway_health_response_defaults_and_serialization():
    """Verify health response construction and round-trip serialization."""
    resp = GatewayHealthResponse(
        status="healthy",
        personaos_connected=True,
        personaos_url="http://127.0.0.1:8000",
        pab_connected=True,
    )
    assert resp.status == "healthy"
    assert resp.gateway_version == "1.0.0"
    assert resp.personaos_connected is True
    assert resp.pab_connected is True
    assert isinstance(resp.timestamp, datetime)

    data = resp.model_dump()
    assert data["status"] == "healthy"
    assert data["gateway_version"] == "1.0.0"

    json_str = resp.model_dump_json()
    restored = GatewayHealthResponse.model_validate_json(json_str)
    assert restored.status == resp.status
    assert restored.gateway_version == resp.gateway_version


def test_gateway_diagnostics_response():
    """Verify diagnostics probe model validation and structure."""
    diag = GatewayDiagnosticsResponse(
        code=ConnectionDiagnosticCode.CONNECTED,
        status="ok",
        latency_ms=12.4,
        upstream_url="http://127.0.0.1:8000",
        cors_supported=True,
        pna_supported=True,
        auth_status="authenticated",
        api_version_match=True,
        details={"ping": "pong"},
    )
    assert diag.code == ConnectionDiagnosticCode.CONNECTED
    assert diag.latency_ms == 12.4
    assert diag.details["ping"] == "pong"

    # Latency cannot be negative
    with pytest.raises(ValidationError):
        GatewayDiagnosticsResponse(
            code=ConnectionDiagnosticCode.NETWORK_UNREACHABLE,
            status="error",
            latency_ms=-1.0,
            upstream_url="http://127.0.0.1:8000",
        )


def test_gateway_worker_summary():
    """Verify workforce summary model."""
    worker = GatewayWorkerSummary(
        id="se",
        name="Software Engineer",
        role="Engineer",
        status="idle",
        current_profile_version=2,
        capabilities=["python", "docker"],
        permissions=["repo:read"],
    )
    assert worker.id == "se"
    assert worker.current_profile_version == 2
    assert "python" in worker.capabilities

    # Profile version must be >= 1
    with pytest.raises(ValidationError):
        GatewayWorkerSummary(
            id="se",
            name="Software Engineer",
            role="Engineer",
            status="idle",
            current_profile_version=0,
        )


def test_gateway_run_create_and_summary():
    """Verify orchestration run request and summary validation."""
    req = GatewayRunCreateRequest(
        run_id="run-123",
        objective="Implement feature",
        coordinator_handle="worker-lead",
    )
    assert req.run_id == "run-123"

    with pytest.raises(ValidationError):
        GatewayRunCreateRequest(run_id="", objective="Empty run ID")

    summary = GatewayRunSummary(
        run_id="run-123",
        objective="Implement feature",
        status="active",
        consumer_generation=0,
    )
    assert summary.consumer_generation == 0
    assert summary.status == "active"


def test_gateway_dag_node_and_edge_aliases():
    """Verify DAG node and edge aliases for serialization compatibility."""
    node = GatewayDagNode(
        task_id="task-01",
        title="Migrate Schema",
        worker_id="se",
        status="completed",
        dependencies=[],
        wave=1,
        generation=2,
    )
    assert node.wave == 1
    assert node.generation == 2

    # Wave and generation must be >= 1
    with pytest.raises(ValidationError):
        GatewayDagNode(
            task_id="task-01",
            title="Migrate Schema",
            status="completed",
            wave=0,
        )

    # Edge creation with 'from' and 'to' aliases
    edge = GatewayDagEdge.model_validate({"from": "task-01", "to": "task-02"})
    assert edge.from_node == "task-01"
    assert edge.to_node == "task-02"

    dumped = edge.model_dump(by_alias=True)
    assert "from" in dumped
    assert "to" in dumped
    assert dumped["from"] == "task-01"
    assert dumped["to"] == "task-02"

    projection = GatewayDagProjection(
        run_id="run-1",
        nodes=[node],
        edges=[edge],
        total_tasks=1,
        completed_tasks=1,
        ready_tasks=0,
    )
    assert len(projection.nodes) == 1
    assert len(projection.edges) == 1


def test_gateway_ready_waves():
    """Verify ready waves response construction and nesting."""
    ready_task = GatewayReadyTask(
        task_id="task-02",
        title="Implement API",
        worker_id="se",
        priority="high",
        generation=1,
    )
    wave = GatewayReadyWave(wave_number=1, ready_tasks=[ready_task])
    waves_resp = GatewayReadyWavesResponse(
        run_id="run-1",
        current_wave=1,
        waves=[wave],
    )
    assert waves_resp.current_wave == 1
    assert len(waves_resp.waves[0].ready_tasks) == 1
    assert waves_resp.waves[0].ready_tasks[0].task_id == "task-02"


def test_gateway_decision_gate_and_fenced_resolve():
    """Verify decision gate contracts and generation fencing."""
    gate = GatewayDecisionGate(
        gate_id="gate-01",
        task_id="task-02",
        title="Security Policy Gate",
        question="Approve token revocation?",
        options=["Approve", "Reject"],
        status="pending",
        execution_generation=1,
        review_context={"detail": "Needs review"},
    )
    assert gate.gate_id == "gate-01"
    assert gate.execution_generation == 1

    # Resolve request with valid decision
    resolve_req = GatewayGateResolveRequest(
        decision="approve",
        expected_generation=1,
        review_notes="Approved by lead",
        selected_option="Approve",
    )
    assert resolve_req.decision == "approve"
    assert resolve_req.expected_generation == 1

    # Invalid decision literal must fail
    with pytest.raises(ValidationError):
        GatewayGateResolveRequest(
            decision="maybe",  # type: ignore[arg-type]
            expected_generation=1,
        )

    # Resolve response
    resolve_resp = GatewayGateResolveResponse(
        gate_id="gate-01",
        task_id="task-02",
        resolved=True,
        status="approved",
        new_generation=2,
    )
    assert resolve_resp.new_generation == 2
    assert resolve_resp.resolved is True


def test_gateway_event_message():
    """Verify SSE event message structure."""
    event = GatewayEventMessage(
        event_id="evt-001",
        event_type="ready_wave_advanced",
        payload={"run_id": "run-1", "wave": 2},
    )
    assert event.event_id == "evt-001"
    assert event.event_type == "ready_wave_advanced"
    assert event.payload["wave"] == 2


def test_openapi_schema_generation():
    """Verify OpenAPI 3.1.0 generation includes all /integration endpoints and components."""
    schema = get_integration_openapi_schema()
    assert "paths" in schema
    assert "components" in schema
    assert "schemas" in schema["components"]

    paths = schema["paths"]
    assert "/integration/health" in paths
    assert "/integration/diagnostics" in paths
    assert "/integration/workers" in paths
    assert "/integration/runs" in paths
    assert "/integration/runs/{run_id}/dag" in paths
    assert "/integration/runs/{run_id}/ready-waves" in paths
    assert "/integration/decision-gates" in paths
    assert "/integration/decision-gates/{gate_id}" in paths
    assert "/integration/decision-gates/{gate_id}/resolve" in paths

    schemas = schema["components"]["schemas"]
    assert "GatewayHealthResponse" in schemas
    assert "GatewayDiagnosticsResponse" in schemas
    assert "GatewayWorkerSummary" in schemas
    assert "GatewayRunCreateRequest" in schemas
    assert "GatewayRunSummary" in schemas
    assert "GatewayDagProjection" in schemas
    assert "GatewayDagNode" in schemas
    assert "GatewayDagEdge" in schemas
    assert "GatewayReadyWavesResponse" in schemas
    assert "GatewayDecisionGate" in schemas
    assert "GatewayGateResolveRequest" in schemas
    assert "GatewayGateResolveResponse" in schemas

    # Test JSON dump helper
    json_text = dump_integration_openapi_schema()
    parsed = json.loads(json_text)
    assert parsed["openapi"] == "3.1.0"
