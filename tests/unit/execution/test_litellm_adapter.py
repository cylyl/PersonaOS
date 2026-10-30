"""Unit tests for LiteLLMAdapter."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from personaos.domain.task import Task
from personaos.domain.worker import Persona, Worker, WorkerProfile
from personaos.execution.adapters.base import Adapter
from personaos.execution.adapters.litellm import LiteLLMAdapter, LiteLLMConfig
from personaos.execution.context import WorkerExecutionContext
from personaos.execution.result import Result


def _make_context(task_input: dict | None = None) -> WorkerExecutionContext:
    worker = Worker(
        id="se",
        name="Software Engineer",
        role="developer",
        status="active",
        runtime="litellm",
        max_concurrent_tasks=1,
        current_profile_version=1,
    )
    profile = WorkerProfile(
        version=1,
        role="developer",
        runtime="litellm",
        skills=["python", "fastapi", "testing"],
        persona=Persona(autonomy="high", behavior={"initiative": "proactive"}),
    )
    task = Task(
        id="task-litellm-01",
        title="Implement Health Route",
        objective="Write a FastAPI health endpoint function",
        input=task_input or {"path": "/healthz"},
        profile_version=1,
        status="queued",
        priority="high",
        assigned_worker="se",
    )
    return WorkerExecutionContext(worker=worker, profile=profile, task=task)


def test_litellm_adapter_conforms_to_protocol():
    adapter = LiteLLMAdapter()
    assert isinstance(adapter, Adapter)


@pytest.mark.asyncio
async def test_litellm_adapter_success_mock():
    adapter = LiteLLMAdapter(
        LiteLLMConfig(
            api_base="http://mock-proxy:4000/v1",
            api_key="mock-key",
            model="qwen2.5-coder-14b",
        )
    )
    context = _make_context()

    fake_response = {
        "id": "chatcmpl-mock",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "```python\ndef healthz():\n    return {'status': 'ok'}\n```",
                },
            }
        ],
        "usage": {
            "prompt_tokens": 40,
            "completion_tokens": 20,
            "total_tokens": 60,
        },
    }

    mock_resp = AsyncMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = fake_response

    with patch("httpx.AsyncClient.post", return_value=mock_resp):
        res = await adapter.execute(context)

    assert isinstance(res, Result)
    assert res.status == "completed"
    assert res.error is None
    assert res.output is not None
    assert "healthz" in res.output["text"]
    assert res.output["model"] == "qwen2.5-coder-14b"
    assert res.output["tokens"]["total_tokens"] == 60


@pytest.mark.asyncio
async def test_litellm_adapter_workspace_file_writing(tmp_path: Path):
    target_rel = "app/health.py"
    context = _make_context(
        task_input={
            "workspace": str(tmp_path),
            "target_file": target_rel,
        }
    )

    adapter = LiteLLMAdapter(
        LiteLLMConfig(
            api_base="http://mock-proxy:4000/v1",
            api_key="mock-key",
            model="qwen2.5-coder-14b",
        )
    )

    code_content = "def healthz():\n    return {'status': 'ok'}\n"
    fake_response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": f"```python\n{code_content}```",
                }
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    }

    mock_resp = AsyncMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = fake_response

    with patch("httpx.AsyncClient.post", return_value=mock_resp):
        res = await adapter.execute(context)

    assert res.status == "completed"
    written_file = tmp_path / target_rel
    assert written_file.exists()
    assert written_file.read_text(encoding="utf-8") == code_content
    assert str(written_file) in res.output["files_written"]


@pytest.mark.asyncio
async def test_litellm_adapter_http_error():
    adapter = LiteLLMAdapter(
        LiteLLMConfig(
            api_base="http://mock-proxy:4000/v1",
            api_key="mock-key",
            model="qwen2.5-coder-14b",
        )
    )
    context = _make_context()

    mock_resp = AsyncMock(spec=httpx.Response)
    mock_resp.status_code = 503
    mock_resp.text = "Service Unavailable"

    with patch("httpx.AsyncClient.post", return_value=mock_resp):
        res = await adapter.execute(context)

    assert res.status == "failed"
    assert "LiteLLM HTTP 503" in (res.error or "")
    assert res.output is None


@pytest.mark.asyncio
async def test_litellm_adapter_live_call():
    """Live integration test against local LiteLLM on port 4000."""
    adapter = LiteLLMAdapter(
        LiteLLMConfig(
            api_base="http://localhost:4000/v1",
            api_key="sk-master-dev-key",
            model="qwen2.5-coder-14b",
            timeout_seconds=30.0,
        )
    )
    context = _make_context(task_input={"instruction": "Return string 'TEST_PASSED'"})

    res = await adapter.execute(context)
    assert res.status == "completed", f"Failed with: {res.error}"
    assert res.output is not None
    assert "TEST_PASSED" in res.output["text"]
    assert res.output["tokens"]["total_tokens"] > 0
    assert res.output["latency_ms"] > 0
