"""Unit tests for PABExecutionAdapter in PersonaOS."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from personaos.domain.task import Task
from personaos.domain.worker import Persona, Worker, WorkerProfile
from personaos.execution.adapters.base import Adapter
from personaos.execution.adapters.pab import PABExecutionAdapter, PABExecutionConfig
from personaos.execution.context import WorkerExecutionContext
from personaos.execution.result import Result


def _make_context(
    task_input: dict | None = None,
    runtime: str = "openclaw",
    generation: int = 1,
) -> WorkerExecutionContext:
    worker = Worker(
        id="backend-dev-01",
        name="Backend Engineer",
        role="developer",
        status="active",
        runtime=runtime,
        max_concurrent_tasks=1,
        current_profile_version=2,
    )
    profile = WorkerProfile(
        version=2,
        role="developer",
        runtime=runtime,
        skills=["python", "fastapi", "docker"],
        persona=Persona(autonomy="high", behavior={"style": "rigorous"}),
    )
    task = Task(
        id="task-pab-001",
        title="Payment Idempotency",
        objective="Implement idempotency check on payments",
        input=task_input or {"workspace": "/tmp"},
        profile_version=2,
        status="in_progress",
        priority="high",
        assigned_worker="backend-dev-01",
    )
    # Set generation attribute
    object.__setattr__(task, "generation", generation)
    return WorkerExecutionContext(worker=worker, profile=profile, task=task)


def test_pab_adapter_conforms_to_protocol():
    adapter = PABExecutionAdapter()
    assert isinstance(adapter, Adapter)


def test_env_sanitization_removes_secrets():
    adapter = PABExecutionAdapter()
    with patch.dict(
        "os.environ",
        {
            "SAFE_VAR": "visible",
            "OPENAI_API_KEY": "sk-secret-12345",
            "DATABASE_PASSWORD": "secret-pass-word",
            "AUTH_BEARER_TOKEN": "bearer-token-val",
        },
        clear=True,
    ):
        sanitized = adapter._sanitize_env()
        assert "SAFE_VAR" in sanitized
        assert "OPENAI_API_KEY" not in sanitized
        assert "DATABASE_PASSWORD" not in sanitized
        assert "AUTH_BEARER_TOKEN" not in sanitized


@pytest.mark.asyncio
async def test_pab_adapter_list_agents():
    adapter = PABExecutionAdapter()
    mock_payload = {
        "contract_version": "1.0",
        "status": "ok",
        "agents": [
            {"provider": "openclaw", "available": True, "detected_version": "2026.10"},
            {"provider": "codex", "available": True, "detected_version": "0.18"},
        ],
    }

    with patch.object(adapter, "_run_pab_cmd", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = mock_payload
        agents = await adapter.list_agents()
        assert len(agents) == 2
        assert agents[0]["provider"] == "openclaw"
        mock_run.assert_called_once_with(["control", "list-agents", "--json"])


@pytest.mark.asyncio
async def test_pab_adapter_execute_success():
    adapter = PABExecutionAdapter(
        PABExecutionConfig(poll_interval_seconds=0.01, timeout_seconds=5.0)
    )
    ctx = _make_context()

    launch_resp = {
        "contract_version": "1.0",
        "status": "ok",
        "execution": {
            "execution_id": "exec-abc-123",
            "status": "running",
            "provider": "openclaw",
        },
    }

    status_resp_running = {
        "contract_version": "1.0",
        "status": "ok",
        "execution": {
            "execution_id": "exec-abc-123",
            "status": "running",
            "provider": "openclaw",
        },
    }

    status_resp_completed = {
        "contract_version": "1.0",
        "status": "ok",
        "execution": {
            "execution_id": "exec-abc-123",
            "status": "completed",
            "exit_code": 0,
            "provider": "openclaw",
            "last_event_sequence": 3,
        },
    }

    with patch.object(adapter, "_run_pab_cmd", new_callable=AsyncMock) as mock_run:
        mock_run.side_effect = [
            launch_resp,
            status_resp_running,
            status_resp_completed,
        ]

        result = await adapter.execute(ctx)
        assert result.status == "completed"
        assert result.error is None
        assert result.output is not None
        assert result.output["execution_id"] == "exec-abc-123"
        assert result.output["exit_code"] == 0
        assert result.new_checkpoint is not None
        assert result.new_checkpoint["last_event_sequence"] == 3


@pytest.mark.asyncio
async def test_pab_adapter_execute_failure():
    adapter = PABExecutionAdapter(
        PABExecutionConfig(poll_interval_seconds=0.01, timeout_seconds=5.0)
    )
    ctx = _make_context()

    launch_resp = {
        "contract_version": "1.0",
        "status": "ok",
        "execution": {"execution_id": "exec-fail-001", "status": "running"},
    }

    status_resp_failed = {
        "contract_version": "1.0",
        "status": "ok",
        "execution": {
            "execution_id": "exec-fail-001",
            "status": "failed",
            "exit_code": 1,
            "error_message": "Process crashed with exit code 1",
        },
    }

    with patch.object(adapter, "_run_pab_cmd", new_callable=AsyncMock) as mock_run:
        mock_run.side_effect = [launch_resp, status_resp_failed]

        result = await adapter.execute(ctx)
        assert result.status == "failed"
        assert "Process crashed" in (result.error or "")


@pytest.mark.asyncio
async def test_pab_adapter_execute_timeout():
    adapter = PABExecutionAdapter(
        PABExecutionConfig(poll_interval_seconds=0.01, timeout_seconds=0.05)
    )
    ctx = _make_context()

    launch_resp = {
        "contract_version": "1.0",
        "status": "ok",
        "execution": {"execution_id": "exec-timeout-001", "status": "running"},
    }

    status_resp_running = {
        "contract_version": "1.0",
        "status": "ok",
        "execution": {"execution_id": "exec-timeout-001", "status": "running"},
    }

    with patch.object(adapter, "_run_pab_cmd", new_callable=AsyncMock) as mock_run:
        mock_run.side_effect = [launch_resp] + [status_resp_running] * 20

        result = await adapter.execute(ctx)
        assert result.status == "failed"
        assert "timed out after" in (result.error or "")


@pytest.mark.asyncio
async def test_pab_adapter_launch_error_isolation():
    adapter = PABExecutionAdapter()
    ctx = _make_context()

    with patch.object(adapter, "_run_pab_cmd", side_effect=RuntimeError("Subprocess missing")):
        result = await adapter.execute(ctx)
        assert result.status == "failed"
        assert "PAB launch invocation failed" in (result.error or "")


@pytest.mark.asyncio
async def test_live_pab_list_agents_if_available():
    adapter = PABExecutionAdapter()
    if not adapter.is_available():
        pytest.skip("PAB CLI is not installed on this host")

    agents = await adapter.list_agents()
    assert isinstance(agents, list)
    assert len(agents) > 0
    provider_names = [a.get("provider") for a in agents]
    assert "openclaw" in provider_names or "agy" in provider_names
