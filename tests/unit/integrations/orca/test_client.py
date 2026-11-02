"""Unit tests for OrcaCLIClient.

Tests subprocess execution, JSON payload parsing, sanitization, timeouts,
and error handling.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personaos.integrations.common.contracts import WorktreeDescriptor
from personaos.integrations.orca.client import OrcaCLIClient
from personaos.integrations.orca.exceptions import (
    OrcaCLIError,
    OrcaTimeoutError,
    OrcaUnavailableError,
    OrcaWorktreeError,
)


@pytest.fixture
def mock_client() -> OrcaCLIClient:
    return OrcaCLIClient(orca_binary="/mock/bin/orca", default_timeout_seconds=5.0)


def test_client_initialization():
    client = OrcaCLIClient(orca_binary="/custom/orca", default_timeout_seconds=10.0)
    assert client.orca_binary == "/custom/orca"


def test_is_available_true(tmp_path: Path):
    dummy_bin = tmp_path / "orca"
    dummy_bin.write_text("#!/bin/sh\nexit 0\n")
    dummy_bin.chmod(0o755)

    client = OrcaCLIClient(orca_binary=str(dummy_bin))
    assert client.is_available() is True


def test_is_available_false(tmp_path: Path):
    dummy_bin = tmp_path / "missing-orca"
    client = OrcaCLIClient(orca_binary=str(dummy_bin))
    assert client.is_available() is False


def test_env_sanitization_removes_secrets(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "top-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-123456")
    monkeypatch.setenv("AUTH_TOKEN", "bearer-xyz")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    client = OrcaCLIClient()
    env = client._get_sanitized_env()

    assert "PATH" in env
    assert "SECRET_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert "AUTH_TOKEN" not in env


@pytest.mark.asyncio
async def test_run_cmd_when_unavailable(mock_client: OrcaCLIClient):
    with patch.object(mock_client, "is_available", return_value=False):
        with pytest.raises(OrcaUnavailableError) as exc_info:
            await mock_client.get_status()
        assert "not found or not executable" in str(exc_info.value)


@pytest.mark.asyncio
async def test_run_cmd_timeout(mock_client: OrcaCLIClient):
    with patch.object(mock_client, "is_available", return_value=True), patch(
        "asyncio.create_subprocess_exec"
    ) as mock_exec:
        proc = AsyncMock()
        proc.communicate.side_effect = asyncio.TimeoutError()
        proc.kill = MagicMock()
        proc.wait = AsyncMock()
        mock_exec.return_value = proc

        with pytest.raises(OrcaTimeoutError) as exc_info:
            await mock_client._run_orca_cmd(["status"])
        assert "timed out after" in str(exc_info.value)


@pytest.mark.asyncio
async def test_run_cmd_invalid_json(mock_client: OrcaCLIClient):
    with patch.object(mock_client, "is_available", return_value=True), patch(
        "asyncio.create_subprocess_exec"
    ) as mock_exec:
        proc = AsyncMock()
        proc.communicate.return_value = (b"not json text", b"")
        proc.returncode = 0
        mock_exec.return_value = proc

        with pytest.raises(OrcaCLIError) as exc_info:
            await mock_client._run_orca_cmd(["status"])
        assert "invalid JSON" in str(exc_info.value)


@pytest.mark.asyncio
async def test_run_cmd_ok_false_raises_clierror(mock_client: OrcaCLIClient):
    with patch.object(mock_client, "is_available", return_value=True), patch(
        "asyncio.create_subprocess_exec"
    ) as mock_exec:
        payload = b'{"ok": false, "error": {"code": "not_found", "message": "Worktree missing"}}'
        proc = AsyncMock()
        proc.communicate.return_value = (payload, b"")
        proc.returncode = 1
        mock_exec.return_value = proc

        with pytest.raises(OrcaCLIError) as exc_info:
            await mock_client._run_orca_cmd(["worktree", "show"])
        assert "Worktree missing" in str(exc_info.value)


@pytest.mark.asyncio
async def test_get_status_ready(mock_client: OrcaCLIClient):
    status_payload = {
        "ok": True,
        "result": {
            "runtime": {
                "state": "ready",
                "reachable": True,
                "runtimeId": "rt-12345",
                "appVersion": "1.4.245",
            }
        },
    }
    with patch.object(mock_client, "_run_orca_cmd", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = status_payload
        res = await mock_client.get_status()
        assert res["runtime"]["state"] == "ready"
        assert res["runtime"]["reachable"] is True


@pytest.mark.asyncio
async def test_get_status_unreachable_raises(mock_client: OrcaCLIClient):
    status_payload = {
        "ok": True,
        "result": {
            "runtime": {
                "state": "offline",
                "reachable": False,
            }
        },
    }
    with patch.object(mock_client, "_run_orca_cmd", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = status_payload
        with pytest.raises(OrcaUnavailableError) as exc_info:
            await mock_client.get_status()
        assert "not ready" in str(exc_info.value)


@pytest.mark.asyncio
async def test_get_current_worktree_normalized(mock_client: OrcaCLIClient):
    payload = {
        "ok": True,
        "result": {
            "worktree": {
                "id": "repo-1::/path/to/worktree",
                "instanceId": "inst-1",
                "repoId": "repo-1",
                "path": "/path/to/worktree",
                "branch": "refs/heads/feature-x",
                "head": "commit-abc",
                "displayName": "feature-x",
                "isMainWorktree": False,
            }
        },
    }
    with patch.object(mock_client, "_run_orca_cmd", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = payload
        wt = await mock_client.get_current_worktree()
        assert isinstance(wt, WorktreeDescriptor)
        assert wt.worktree_id == "repo-1::/path/to/worktree"
        assert wt.display_name == "feature-x"
        assert wt.branch == "refs/heads/feature-x"
        assert wt.path == Path("/path/to/worktree")


@pytest.mark.asyncio
async def test_create_and_wait_terminal(mock_client: OrcaCLIClient):
    create_payload = {
        "ok": True,
        "result": {
            "terminal": {
                "handle": "term_999",
                "processId": 12345,
                "worktreeId": "wt-1",
            }
        },
    }
    wait_payload = {
        "ok": True,
        "result": {"terminal": {"handle": "term_999", "exited": True}},
    }
    read_payload = {
        "ok": True,
        "result": {
            "terminal": {
                "handle": "term_999",
                "tail": ["line 1", "task output success"],
            }
        },
    }

    with patch.object(mock_client, "_run_orca_cmd", new_callable=AsyncMock) as mock_run:
        mock_run.side_effect = [create_payload, wait_payload, read_payload]

        term = await mock_client.create_terminal(
            worktree_selector="active",
            command="python3 -c 'print(1)'",
            title="test-title",
        )
        assert term["handle"] == "term_999"

        await mock_client.wait_terminal("term_999", condition="exit", timeout_ms=5000)
        lines = await mock_client.read_terminal("term_999")
        assert len(lines) == 2
        assert "task output success" in lines[1]
