"""Unit tests for PersonaOSOrcaPlugin SSE event streaming and auto-reconnection (ADR 0041 M6).

Verifies stream parsing, Last-Event-ID resumption, exponential backoff reconnection,
and Gateway API helper methods.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from personaos.api.routes.integration import router as integration_router
from personaos.integrations.common.gateway_schemas import (
    GatewayDiagnosticsResponse,
    GatewayEventMessage,
    GatewayGateResolveResponse,
    GatewayHealthResponse,
)
from personaos.integrations.orca.plugin import (
    PersonaOSOrcaPlugin,
    PersonaOSOrcaPluginError,
)


@pytest.fixture
def app() -> FastAPI:
    """FastAPI app mounting integration router."""
    app = FastAPI(title="Integration SSE Test App")
    app.include_router(integration_router)
    return app


@pytest.mark.asyncio
async def test_plugin_gateway_health(monkeypatch):
    """get_health returns GatewayHealthResponse model."""
    plugin = PersonaOSOrcaPlugin(api_base_url="http://test-gateway:8000")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "status": "healthy",
        "gateway_version": "1.0.0",
        "personaos_connected": True,
        "personaos_url": "http://127.0.0.1:8000",
        "pab_connected": True,
        "timestamp": datetime.now(UTC).isoformat(),
    }

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        health = await plugin.get_health()

        assert isinstance(health, GatewayHealthResponse)
        assert health.status == "healthy"
        assert health.personaos_connected is True
        assert health.pab_connected is True


@pytest.mark.asyncio
async def test_plugin_gateway_diagnostics(monkeypatch):
    """get_diagnostics returns GatewayDiagnosticsResponse model."""
    plugin = PersonaOSOrcaPlugin(api_base_url="http://test-gateway:8000")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "code": "CONNECTED",
        "status": "ok",
        "latency_ms": 1.25,
        "upstream_url": "http://127.0.0.1:8000",
        "cors_supported": True,
        "pna_supported": True,
        "auth_status": "authenticated",
        "api_version_match": True,
        "details": {"database": "ok"},
    }

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        diag = await plugin.get_diagnostics(target_url="http://127.0.0.1:8000")

        assert isinstance(diag, GatewayDiagnosticsResponse)
        assert diag.code == "CONNECTED"
        assert diag.status == "ok"
        assert diag.cors_supported is True


@pytest.mark.asyncio
async def test_plugin_gateway_runs_and_dag():
    """list_runs, create_run, get_run_dag, get_run_ready_waves work as expected."""
    plugin = PersonaOSOrcaPlugin(api_base_url="http://test-gateway:8000")

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get, \
         patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:

        mock_get.return_value = MagicMock(status_code=200, json=lambda: [{"run_id": "run-1"}])
        runs = await plugin.list_runs()
        assert len(runs) == 1
        assert runs[0]["run_id"] == "run-1"

        mock_post.return_value = MagicMock(
            status_code=201, json=lambda: {"run_id": "run-new", "objective": "Coordination"}
        )
        created = await plugin.create_run(run_id="run-new", objective="Coordination")
        assert created["run_id"] == "run-new"

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "run_id": "run-new",
                "nodes": [],
                "edges": [],
                "total_tasks": 0,
                "completed_tasks": 0,
                "ready_tasks": 0,
            },
        )
        dag = await plugin.get_run_dag("run-new")
        assert dag["run_id"] == "run-new"


@pytest.mark.asyncio
async def test_plugin_gateway_decision_gates():
    """list_decision_gates, get_decision_gate, and resolve_decision_gate."""
    plugin = PersonaOSOrcaPlugin(api_base_url="http://test-gateway:8000")

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get, \
         patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: [
                {"gate_id": "gate-1", "task_id": "t-1", "question": "Review canary?"}
            ],
        )
        gates = await plugin.list_decision_gates()
        assert len(gates) == 1
        assert gates[0]["gate_id"] == "gate-1"

        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "gate_id": "gate-1",
                "task_id": "t-1",
                "resolved": True,
                "status": "approved",
                "new_generation": 2,
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )
        resolve_res = await plugin.resolve_decision_gate(
            gate_id="gate-1",
            decision="approve",
            expected_generation=1,
            review_notes="Approved",
        )
        assert isinstance(resolve_res, GatewayGateResolveResponse)
        assert resolve_res.resolved is True
        assert resolve_res.status == "approved"
        assert resolve_res.new_generation == 2


@pytest.mark.asyncio
async def test_plugin_gateway_publish_event():
    """publish_event successfully emits to /integration/events/publish."""
    plugin = PersonaOSOrcaPlugin(api_base_url="http://test-gateway:8000")

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "event_id": "evt-pub-1",
                "event_type": "ready_wave_advanced",
                "payload": {"current_wave": 2},
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )
        published = await plugin.publish_event(
            event_type="ready_wave_advanced",
            payload={"current_wave": 2},
            event_id="evt-pub-1",
        )
        assert isinstance(published, GatewayEventMessage)
        assert published.event_id == "evt-pub-1"
        assert published.event_type == "ready_wave_advanced"


@pytest.mark.asyncio
async def test_plugin_stream_events_parsing():
    """stream_events parses SSE frames (id, event, data, heartbeat) into GatewayEventMessage."""
    plugin = PersonaOSOrcaPlugin(api_base_url="http://test-gateway:8000")

    # Simulate SSE response chunk stream line by line
    sse_lines = [
        ": connected",
        "",
        "id: evt-001",
        "event: task_status_changed",
        'data: {"event_id": "evt-001", "event_type": "task_status_changed", '
        '"payload": {"task_id": "t-1", "status": "completed"}}',
        "",
        "event: heartbeat",
        'data: {"timestamp": "2026-11-01T12:00:00Z"}',
        "",
        "id: evt-002",
        "event: gate_created",
        'data: {"event_id": "evt-002", "event_type": "gate_created", '
        '"payload": {"gate_id": "g-1"}}',
        "",
    ]

    async def mock_aiter_lines():
        for line in sse_lines:
            yield line

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.aiter_lines = mock_aiter_lines

    class MockStreamContext:
        async def __aenter__(self):
            return mock_resp

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    with patch("httpx.AsyncClient.stream", return_value=MockStreamContext()):
        events: list[GatewayEventMessage] = []
        async for evt in plugin.stream_events(max_events=3):
            events.append(evt)

        assert len(events) == 3
        assert events[0].event_id == "evt-001"
        assert events[0].event_type == "task_status_changed"
        assert events[0].payload["status"] == "completed"

        assert events[1].event_type == "heartbeat"

        assert events[2].event_id == "evt-002"
        assert events[2].event_type == "gate_created"


@pytest.mark.asyncio
async def test_plugin_stream_events_auto_reconnect_with_last_event_id():
    """When stream disconnects, auto_reconnect triggers with Last-Event-ID resumption."""
    plugin = PersonaOSOrcaPlugin(api_base_url="http://test-gateway:8000")

    stream_1_lines = [
        ": connected",
        "",
        "id: evt-101",
        "event: task_status_changed",
        'data: {"event_id": "evt-101", "event_type": "task_status_changed", '
        '"payload": {"step": 1}}',
        "",
    ]
    stream_2_lines = [
        ": connected",
        "",
        "id: evt-102",
        "event: task_status_changed",
        'data: {"event_id": "evt-102", "event_type": "task_status_changed", '
        '"payload": {"step": 2}}',
        "",
    ]

    call_count = 0
    passed_last_ids = []

    class MockStreamContext:
        def __init__(self, headers, params):
            self.headers = headers
            self.params = params

        async def __aenter__(self):
            nonlocal call_count
            call_count += 1
            last_id_hdr = self.headers.get("Last-Event-ID")
            last_id_param = self.params.get("last_event_id")
            passed_last_ids.append(last_id_hdr or last_id_param)

            mock_resp = MagicMock()
            mock_resp.status_code = 200

            if call_count == 1:
                async def gen1():
                    for line in stream_1_lines:
                        yield line
                    raise ConnectionResetError("Simulated socket drop")
                mock_resp.aiter_lines = gen1
            else:
                async def gen2():
                    for line in stream_2_lines:
                        yield line
                mock_resp.aiter_lines = gen2
            return mock_resp

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    def mock_stream(method, url, params=None, headers=None, **kwargs):
        return MockStreamContext(headers or {}, params or {})

    with patch("httpx.AsyncClient.stream", side_effect=mock_stream), \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:

        events: list[GatewayEventMessage] = []
        async for evt in plugin.stream_events(
            auto_reconnect=True,
            max_reconnect_attempts=3,
            initial_backoff=0.01,
            max_events=2,
        ):
            events.append(evt)

        assert len(events) == 2
        assert events[0].event_id == "evt-101"
        assert events[1].event_id == "evt-102"
        assert call_count == 2
        # Second call carried Last-Event-ID = evt-101
        assert passed_last_ids[1] == "evt-101"
        mock_sleep.assert_called_once()


@pytest.mark.asyncio
async def test_plugin_stream_events_exceeds_max_retries():
    """When stream repeatedly fails, raises error after max_reconnect_attempts."""
    plugin = PersonaOSOrcaPlugin(api_base_url="http://test-gateway:8000")

    class FailingStreamContext:
        async def __aenter__(self):
            raise ConnectionRefusedError("Gateway unreachable")

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    with patch("httpx.AsyncClient.stream", return_value=FailingStreamContext()), \
         patch("asyncio.sleep", new_callable=AsyncMock):

        with pytest.raises(PersonaOSOrcaPluginError) as exc_info:
            async for _ in plugin.stream_events(
                auto_reconnect=True,
                max_reconnect_attempts=2,
                initial_backoff=0.01,
            ):
                pass

        assert "SSE stream failed" in str(exc_info.value)
