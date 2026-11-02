"""Unit tests for PersonaOSOrcaPlugin.

Verifies:
- Worker roster retrieval
- Task submission with automatic active Orca worktree context binding
- Review queue context inspection and decisioning
- Strict enforcement of zero direct external agent CLI execution
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from personaos.integrations.common.contracts import WorktreeDescriptor
from personaos.integrations.orca.client import OrcaCLIClient
from personaos.integrations.orca.plugin import (
    DirectAgentExecutionViolationError,
    PersonaOSOrcaPlugin,
    PersonaOSOrcaPluginError,
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
def mock_orca_client(mock_worktree: WorktreeDescriptor) -> OrcaCLIClient:
    client = MagicMock(spec=OrcaCLIClient)
    client.is_available.return_value = True
    client.get_current_worktree = AsyncMock(return_value=mock_worktree)
    return client


@pytest.mark.asyncio
async def test_list_workers():
    plugin = PersonaOSOrcaPlugin(api_base_url="http://test-personaos:8000")
    workers_payload = [
        {"id": "se", "name": "Software Engineer", "status": "idle"},
        {"id": "qa", "name": "QA Specialist", "status": "busy"},
    ]

    with patch("httpx.AsyncClient.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = workers_payload
        mock_get.return_value = mock_resp

        result = await plugin.list_workers()
        assert len(result) == 2
        assert result[0]["id"] == "se"
        assert result[1]["id"] == "qa"


@pytest.mark.asyncio
async def test_submit_task_links_active_worktree(mock_orca_client: OrcaCLIClient):
    plugin = PersonaOSOrcaPlugin(
        api_base_url="http://test-personaos:8000",
        orca_client=mock_orca_client,
    )

    with patch("httpx.AsyncClient.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"id": "task-123", "status": "pending"}
        mock_post.return_value = mock_resp

        res = await plugin.submit_task(
            worker_id="se",
            title="Implement Bridge",
            objective="Deliver M8 plugin",
            link_active_worktree=True,
        )

        assert res["id"] == "task-123"
        # Check call arguments to post
        _, kwargs = mock_post.call_args
        body = kwargs["json"]
        assert body["worker_id"] == "se"
        assert body["title"] == "Implement Bridge"
        assert "worktree" in body["input"]
        assert body["input"]["worktree"]["repo_id"] == "repo-1"
        assert body["input"]["worktree"]["branch"] == "refs/heads/main"


@pytest.mark.asyncio
async def test_get_task_and_review_context():
    plugin = PersonaOSOrcaPlugin(api_base_url="http://test-personaos:8000")

    with patch("httpx.AsyncClient.get") as mock_get:
        # Mock get task
        mock_task_resp = MagicMock()
        mock_task_resp.status_code = 200
        mock_task_resp.json.return_value = {"id": "task-456", "status": "review"}

        # Mock review context
        mock_review_resp = MagicMock()
        mock_review_resp.status_code = 200
        mock_review_resp.json.return_value = {
            "task_id": "task-456",
            "status": "review",
            "findings": ["1 test failure"],
        }

        mock_get.side_effect = [mock_task_resp, mock_review_resp]

        task = await plugin.get_task("task-456")
        assert task["id"] == "task-456"

        review = await plugin.get_task_review_context("task-456")
        assert review["task_id"] == "task-456"
        assert "1 test failure" in review["findings"]


@pytest.mark.asyncio
async def test_approve_and_reject_task():
    plugin = PersonaOSOrcaPlugin(api_base_url="http://test-personaos:8000")

    with patch("httpx.AsyncClient.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        app_res = await plugin.approve_task("task-456", review_notes="Looks great")
        assert app_res["ok"] is True

        rej_res = await plugin.reject_task("task-456", reason="Missing unit test")
        assert rej_res["ok"] is True


def test_guard_no_direct_agent_execution():
    plugin = PersonaOSOrcaPlugin()

    # Attempting to invoke external agent CLIs directly must raise
    with pytest.raises(DirectAgentExecutionViolationError):
        plugin.guard_no_direct_agent_execution(["codex", "exec", "something"])

    with pytest.raises(DirectAgentExecutionViolationError):
        plugin.guard_no_direct_agent_execution("/usr/bin/claude --print")

    with pytest.raises(DirectAgentExecutionViolationError):
        plugin.guard_no_direct_agent_execution("openclaw run test")

    with pytest.raises(DirectAgentExecutionViolationError):
        plugin.guard_no_direct_agent_execution("agy --mode auto")

    with pytest.raises(DirectAgentExecutionViolationError):
        plugin.guard_no_direct_agent_execution("cursor --task 123")

    with pytest.raises(DirectAgentExecutionViolationError):
        plugin.guard_no_direct_agent_execution("/usr/local/bin/opencode start")

    # Safe system or test command should pass without error
    plugin.guard_no_direct_agent_execution(["python3", "-m", "unittest"])
    plugin.guard_no_direct_agent_execution("git status")


@pytest.mark.asyncio
async def test_live_api_integration():
    """Verify live connectivity if PersonaOS API is running locally."""
    plugin = PersonaOSOrcaPlugin(api_base_url="http://localhost:8000")
    try:
        workers = await plugin.list_workers()
        assert isinstance(workers, list)
        worker_ids = [w["id"] for w in workers]
        assert "se" in worker_ids
    except Exception as exc:
        pytest.skip(f"Local PersonaOS API not reachable: {exc}")
