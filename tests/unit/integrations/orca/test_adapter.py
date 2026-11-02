"""Unit tests for OrcaExecutionAdapter.

Verifies worktree resolution, mock agent execution, terminal output capture,
timeout handling, duplicate execution prevention, and kernel ownership boundaries.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personaos.integrations.common.contracts import (
    AgentDescriptor,
    ExecutionEvent,
    ExecutionRequest,
    ExecutionResult,
    ExecutionStatus,
    WorktreeDescriptor,
)
from personaos.integrations.orca.adapter import OrcaExecutionAdapter
from personaos.integrations.orca.client import OrcaCLIClient
from personaos.integrations.orca.exceptions import (
    OrcaError,
    OrcaTimeoutError,
    OrcaUnavailableError,
    OrcaWorktreeError,
)


@pytest.fixture
def mock_worktree() -> WorktreeDescriptor:
    return WorktreeDescriptor(
        worktree_id="repo-1::/workspace/test",
        instance_id="inst-1",
        repo_id="repo-1",
        path=Path("/workspace/test"),
        branch="refs/heads/main",
        head="a1b2c3d4",
        display_name="test-worktree",
    )


@pytest.fixture
def mock_client(mock_worktree: WorktreeDescriptor) -> OrcaCLIClient:
    client = MagicMock(spec=OrcaCLIClient)
    client.is_available.return_value = True
    client.get_current_worktree = AsyncMock(return_value=mock_worktree)
    client.show_worktree = AsyncMock(return_value=mock_worktree)
    client.create_terminal = AsyncMock(
        return_value={"handle": "term_test_1", "processId": 12345}
    )
    client.wait_terminal = AsyncMock(return_value={"ok": True})
    client.read_terminal = AsyncMock(
        return_value=["Executing mock worker task", "Step 1 complete", "Final result: SUCCESS"]
    )
    client.show_terminal = AsyncMock(return_value={"preview": "Final result: SUCCESS"})
    client.close_terminal = AsyncMock(return_value=True)
    return client


@pytest.fixture
def sample_request() -> ExecutionRequest:
    return ExecutionRequest(
        task_id="task-001",
        worker_id="backend-java",
        profile_version=1,
        agent=AgentDescriptor(kind="mock"),
        target_worktree="active",
        command="python3 -c 'print(\"Task completed successfully\")'",
        timeout_seconds=10.0,
        skills=["spring-boot"],
    )


@pytest.mark.asyncio
async def test_resolve_worktree_success(mock_client: OrcaCLIClient, mock_worktree: WorktreeDescriptor):
    adapter = OrcaExecutionAdapter(client=mock_client)
    wt = await adapter.resolve_worktree("active")
    assert wt.worktree_id == mock_worktree.worktree_id
    assert wt.display_name == "test-worktree"


@pytest.mark.asyncio
async def test_resolve_worktree_failure(mock_client: OrcaCLIClient):
    mock_client.get_current_worktree.side_effect = OrcaWorktreeError("No active worktree")
    adapter = OrcaExecutionAdapter(client=mock_client)

    req = ExecutionRequest(
        task_id="task-002",
        worker_id="qa-engineer",
        profile_version=1,
        agent=AgentDescriptor(kind="mock"),
    )

    result = await adapter.execute(req)
    assert result.status == ExecutionStatus.FAILED
    assert result.exit_code == 1
    assert "Worktree resolution error" in (result.error_message or "")


@pytest.mark.asyncio
async def test_execute_mock_agent_success(
    mock_client: OrcaCLIClient,
    sample_request: ExecutionRequest,
):
    adapter = OrcaExecutionAdapter(client=mock_client)
    events: list[ExecutionEvent] = []

    result = await adapter.execute(sample_request, event_callback=events.append)

    assert result.status == ExecutionStatus.COMPLETED
    assert result.exit_code == 0
    assert result.request_id == sample_request.request_id
    assert result.handle.native_terminal_id == "term_test_1"
    assert "Final result: SUCCESS" in result.raw_terminal_output
    assert mock_client.create_terminal.called
    assert mock_client.wait_terminal.called
    assert mock_client.close_terminal.called

    # Check event sequence: PREPARING -> RUNNING -> COMPLETED
    assert len(events) >= 3
    assert events[0].status == ExecutionStatus.PREPARING
    assert events[1].status == ExecutionStatus.RUNNING
    assert events[2].status == ExecutionStatus.COMPLETED


@pytest.mark.asyncio
async def test_execute_agent_failure_detected(
    mock_client: OrcaCLIClient,
    sample_request: ExecutionRequest,
):
    # Simulate a python traceback in terminal output
    mock_client.read_terminal.return_value = [
        "Traceback (most recent call last):",
        "  File 'mock_agent.py', line 10, in <module>",
        "ZeroDivisionError: division by zero",
    ]
    adapter = OrcaExecutionAdapter(client=mock_client)

    result = await adapter.execute(sample_request)

    assert result.status == ExecutionStatus.FAILED
    assert result.exit_code == 1
    assert "failure" in (result.error_message or "").lower()


@pytest.mark.asyncio
async def test_execute_timeout_handling(
    mock_client: OrcaCLIClient,
    sample_request: ExecutionRequest,
):
    mock_client.wait_terminal.side_effect = OrcaTimeoutError("Terminal wait timed out")
    adapter = OrcaExecutionAdapter(client=mock_client)

    result = await adapter.execute(sample_request)

    assert result.status == ExecutionStatus.TIMEOUT
    assert result.exit_code == 124
    assert "timed out" in (result.error_message or "").lower()


@pytest.mark.asyncio
async def test_duplicate_execution_rejected(
    mock_client: OrcaCLIClient,
    sample_request: ExecutionRequest,
):
    adapter = OrcaExecutionAdapter(client=mock_client)

    # Make wait_terminal hang until we test duplicate
    wait_event = asyncio.Event()

    async def slow_wait(*args, **kwargs):
        await wait_event.wait()
        return {"ok": True}

    mock_client.wait_terminal.side_effect = slow_wait

    # Start first execution in background
    task1 = asyncio.create_task(adapter.execute(sample_request))
    await asyncio.sleep(0.05)  # Let it acquire lock and insert in _active_executions

    # Attempt second execution with same request
    with pytest.raises(OrcaError) as exc_info:
        await adapter.execute(sample_request)
    assert "Duplicate execution rejected" in str(exc_info.value)

    # Release first task
    wait_event.set()
    res1 = await task1
    assert res1.status == ExecutionStatus.COMPLETED


@pytest.mark.asyncio
async def test_kernel_ownership_restriction(
    mock_client: OrcaCLIClient,
    sample_request: ExecutionRequest,
):
    """Verify that adapter only returns ExecutionResult and cannot alter PersonaOS task states or database."""
    adapter = OrcaExecutionAdapter(client=mock_client)

    # Execute request
    result = await adapter.execute(sample_request)

    # Invariant: Adapter returns ExecutionResult. It has no references to DB sessions,
    # nor does it call complete_task, transition_task, or claim_task.
    assert isinstance(result, ExecutionResult)
    assert not hasattr(adapter, "complete_task")
    assert not hasattr(adapter, "claim_task")
    assert not hasattr(adapter, "session")
