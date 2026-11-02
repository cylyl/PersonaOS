"""Integration tests for PersonaOS–Orca Execution Bridge.

Tests live interaction with the Orca CLI and daemon:
- Daemon connectivity and status reporting
- Worktree resolution and metadata extraction
- Repository listing
- Real terminal lifecycle (spawn, read output, close)
- End-to-end ExecutionAdapter execution with mock worker agent commands
- Error detection on non-zero agent exit

Can be run locally when Orca is active. Skips gracefully if Orca CLI
or daemon is not available.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from personaos.integrations.common.contracts import (
    AgentDescriptor,
    ExecutionEvent,
    ExecutionRequest,
    ExecutionStatus,
)
from personaos.integrations.orca.adapter import OrcaExecutionAdapter
from personaos.integrations.orca.client import OrcaCLIClient


def _is_orca_daemon_running() -> bool:
    """Check if Orca CLI is available and reachable."""
    client = OrcaCLIClient()
    if not client.is_available():
        return False
    return True


orca_live = pytest.mark.skipif(
    not _is_orca_daemon_running(),
    reason="Orca CLI or daemon is not available on this host",
)


@pytest.fixture
def orca_client() -> OrcaCLIClient:
    return OrcaCLIClient()


@pytest.fixture
def orca_adapter(orca_client: OrcaCLIClient) -> OrcaExecutionAdapter:
    return OrcaExecutionAdapter(client=orca_client)


@orca_live
@pytest.mark.asyncio
async def test_live_orca_status(orca_client: OrcaCLIClient):
    """Verify live Orca daemon responds to status command."""
    status = await orca_client.get_status()
    assert isinstance(status, dict)
    app = status.get("app", {})
    runtime = status.get("runtime", {})
    assert app.get("running") is True
    assert runtime.get("state") == "ready"
    assert runtime.get("reachable") is True


@orca_live
@pytest.mark.asyncio
async def test_live_orca_worktree_current(orca_client: OrcaCLIClient):
    """Verify current worktree resolution against live Orca workspace."""
    wt = await orca_client.get_current_worktree()
    assert wt is not None
    assert str(wt.path) == os.getcwd() or "yabby" in str(wt.path)
    assert wt.repo_id is not None
    assert wt.branch is not None


@orca_live
@pytest.mark.asyncio
async def test_live_orca_repo_list(orca_client: OrcaCLIClient):
    """Verify listing repositories via live Orca CLI."""
    repos = await orca_client.list_repos()
    assert isinstance(repos, list)
    assert len(repos) > 0


@orca_live
@pytest.mark.asyncio
async def test_live_terminal_lifecycle(orca_client: OrcaCLIClient):
    """Verify creating, waiting for, and closing a terminal session."""
    wt = await orca_client.get_current_worktree()
    cmd = 'python3 -c "print(\'PERSONAOS_ORCA_INTEGRATION_TOKEN_42\')"\nexit 0'
    
    term_res = await orca_client.create_terminal(
        worktree_selector=wt.worktree_id,
        title="personaos-integration-test-lifecycle",
        command=cmd,
    )
    handle = term_res.get("handle") or term_res.get("id")
    assert handle is not None

    try:
        wait_res = await orca_client.wait_terminal(handle, condition="exit", timeout_ms=10000)
        assert wait_res.get("ok") is True
        show_data = await orca_client.show_terminal(handle)
        assert "handle" in show_data
    finally:
        # Always close terminal
        close_res = await orca_client.close_terminal(handle)
        assert close_res is True


@orca_live
@pytest.mark.asyncio
async def test_live_adapter_execute_mock_agent_success(orca_adapter: OrcaExecutionAdapter):
    """Verify OrcaExecutionAdapter completes a successful task execution."""
    events: list[ExecutionEvent] = []

    request = ExecutionRequest(
        task_id="task-live-integ-001",
        worker_id="worker-qa-01",
        profile_version=1,
        agent=AgentDescriptor(kind="custom"),
        command='python3 -c "import sys; print(\'INTEG_TASK_COMPLETE_OK\'); sys.exit(0)"',
        target_worktree=os.getcwd(),
        timeout_seconds=30,
    )

    result = await orca_adapter.execute(request, event_callback=events.append)

    assert result.request_id == request.request_id
    assert result.status == ExecutionStatus.COMPLETED
    assert result.exit_code == 0
    assert result.handle is not None
    assert result.handle.native_terminal_id is not None
    assert "INTEG_TASK_COMPLETE_OK" in result.raw_terminal_output or "INTEG_TASK_COMPLETE_OK" in result.output_summary

    # Verify lifecycle events were emitted
    statuses = [ev.status for ev in events]
    assert ExecutionStatus.RUNNING in statuses
    assert ExecutionStatus.COMPLETED in statuses


@orca_live
@pytest.mark.asyncio
async def test_live_adapter_execute_agent_failure(orca_adapter: OrcaExecutionAdapter):
    """Verify OrcaExecutionAdapter detects non-zero exit and reports failure."""
    request = ExecutionRequest(
        task_id="task-live-integ-002",
        worker_id="worker-qa-02",
        profile_version=1,
        agent=AgentDescriptor(kind="custom"),
        command='python3 -c "import sys; sys.stderr.write(\'INTEG_SIMULATED_FAIL\'); sys.exit(5)"',
        target_worktree=os.getcwd(),
        timeout_seconds=30,
    )

    result = await orca_adapter.execute(request)

    assert result.request_id == request.request_id
    assert result.status == ExecutionStatus.FAILED
    assert result.exit_code in (1, 5)


@pytest.mark.asyncio
async def test_bridge_gateway_sse_stream_and_reconnection():
    """Verify PersonaOSOrcaPlugin SSE stream connects to Integration Gateway with Last-Event-ID resumption."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from personaos.api.routes.integration import router as integration_router
    from personaos.integrations.common.event_bus import gateway_event_bus

    app = FastAPI()
    app.include_router(integration_router)

    gateway_event_bus.clear()
    gateway_event_bus.publish_task_status_changed(
        task_id="t-bridge-1", status="running", event_id="evt-br-1"
    )
    gateway_event_bus.publish_ready_wave_advanced(
        run_id="default", current_wave=2, event_id="evt-br-2"
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://bridge-test") as test_client:
        headers = {"Last-Event-ID": "evt-br-1"}
        async with test_client.stream(
            "GET", "/integration/events?max_events=1", headers=headers
        ) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]
            lines = [line async for line in resp.aiter_lines() if line]
            assert any("evt-br-2" in l for l in lines)
            assert not any("evt-br-1" in l for l in lines)

