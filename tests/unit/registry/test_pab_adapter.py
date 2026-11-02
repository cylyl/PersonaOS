"""Unit tests for PABSyncAdapter (PersonaOS-side external agent bridge adapter).

Tests subprocess execution handling, environment sanitization, validation rules,
timeout protection, dry-run mode, and structured result normalization with mocks.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personaos.registry.pab_adapter import (
    PABSyncAdapter,
    SyncOperationResult,
    SyncStatus,
    TargetSyncResult,
)


@pytest.fixture
def temp_profile_file(tmp_path: Path) -> Path:
    """Create a temporary valid profile v1.yaml."""
    worker_dir = tmp_path / "worker-test"
    worker_dir.mkdir(parents=True)
    p_file = worker_dir / "v1.yaml"
    p_file.write_text(
        """
version: 1
role: developer
description: Test developer worker
skills:
  - python
permissions:
  - repo.read
""",
        encoding="utf-8",
    )
    return p_file


@pytest.fixture
def mock_adapter() -> PABSyncAdapter:
    """Create adapter pointing to dummy binary."""
    return PABSyncAdapter(
        pab_binary="/mock/bin/pab",
        pab_home=Path("/mock/.pab"),
        timeout_seconds=5.0,
    )


def test_adapter_initialization():
    adapter = PABSyncAdapter(
        pab_binary="/usr/bin/custom-pab",
        pab_home=Path("/tmp/pab-home"),
        timeout_seconds=15.0,
    )
    assert adapter.pab_binary == "/usr/bin/custom-pab"
    assert adapter.pab_home == Path("/tmp/pab-home").resolve()


def test_is_available_true(tmp_path: Path):
    dummy_bin = tmp_path / "pab"
    dummy_bin.write_text("#!/bin/sh\nexit 0\n")
    dummy_bin.chmod(0o755)

    adapter = PABSyncAdapter(pab_binary=str(dummy_bin))
    assert adapter.is_available() is True


def test_is_available_false(tmp_path: Path):
    dummy_bin = tmp_path / "nonexistent-pab"
    adapter = PABSyncAdapter(pab_binary=str(dummy_bin))
    assert adapter.is_available() is False


def test_env_sanitization_strips_secrets(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "super-secret")
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-token-12345")
    monkeypatch.setenv("AUTH_BEARER", "token-xyz")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    adapter = PABSyncAdapter()
    clean_env = adapter._sanitize_env()

    assert "PATH" in clean_env
    assert "SECRET_KEY" not in clean_env
    assert "MINIMAX_API_KEY" not in clean_env
    assert "AUTH_BEARER" not in clean_env
    assert clean_env["PAB_HOME"] == str(adapter.pab_home)


def test_validation_invalid_worker_id(temp_profile_file: Path, mock_adapter: PABSyncAdapter):
    errors = mock_adapter._validate_inputs(
        worker_id="bad id with spaces!",
        profile_path=temp_profile_file,
        profile_version=1,
        target_providers=["openclaw"],
    )
    assert len(errors) == 1
    assert "Invalid worker_id" in errors[0]


def test_validation_invalid_version(temp_profile_file: Path, mock_adapter: PABSyncAdapter):
    errors = mock_adapter._validate_inputs(
        worker_id="worker-01",
        profile_path=temp_profile_file,
        profile_version=0,
        target_providers=["openclaw"],
    )
    assert len(errors) == 1
    assert "Invalid profile_version" in errors[0]


def test_validation_missing_file(tmp_path: Path, mock_adapter: PABSyncAdapter):
    missing_file = tmp_path / "nonexistent.yaml"
    errors = mock_adapter._validate_inputs(
        worker_id="worker-01",
        profile_path=missing_file,
        profile_version=1,
        target_providers=["openclaw"],
    )
    assert len(errors) == 1
    assert "Profile path does not exist" in errors[0]


def test_validation_unsupported_provider(temp_profile_file: Path, mock_adapter: PABSyncAdapter):
    errors = mock_adapter._validate_inputs(
        worker_id="worker-01",
        profile_path=temp_profile_file,
        profile_version=1,
        target_providers=["unsupported_runtime"],
    )
    assert len(errors) == 1
    assert "Unsupported target provider" in errors[0]


@pytest.mark.asyncio
async def test_sync_worker_when_pab_unavailable(temp_profile_file: Path, mock_adapter: PABSyncAdapter):
    with patch.object(mock_adapter, "is_available", return_value=False):
        res = await mock_adapter.sync_worker_profile(
            worker_id="worker-01",
            profile_path=temp_profile_file,
            profile_version=1,
        )
        assert res.overall_status == SyncStatus.UNAVAILABLE
        assert res.exit_code == 127
        assert "unavailable" in res.stderr_summary.lower()


@pytest.mark.asyncio
async def test_sync_worker_dry_run_success(temp_profile_file: Path, mock_adapter: PABSyncAdapter):
    mock_plan_json = """
    {
        "plan_id": "plan-1234",
        "operations": [
            {
                "resource_id": "worker-01",
                "target_provider": "openclaw",
                "action": "create"
            },
            {
                "resource_id": "worker-01",
                "target_provider": "agy",
                "action": "update"
            }
        ]
    }
    """
    with patch.object(mock_adapter, "is_available", return_value=True), patch.object(
        mock_adapter, "_execute_subprocess", new_callable=AsyncMock
    ) as mock_exec:
        mock_exec.return_value = (0, mock_plan_json, "")

        res = await mock_adapter.sync_worker_profile(
            worker_id="worker-01",
            profile_path=temp_profile_file,
            profile_version=1,
            target_providers=["openclaw", "agy"],
            dry_run=True,
        )

        assert res.overall_status == SyncStatus.SUCCESS
        assert res.dry_run is True
        assert len(res.targets) == 2
        assert res.targets["openclaw"].action == "planned"
        assert res.targets["agy"].action == "planned"
        assert mock_exec.call_count == 1
        assert "sync" in mock_exec.call_args[0][0]
        assert "plan" in mock_exec.call_args[0][0]


@pytest.mark.asyncio
async def test_sync_worker_live_success(temp_profile_file: Path, mock_adapter: PABSyncAdapter):
    mock_deploy_json = """
    {
        "openclaw": {
            "success": true,
            "result": {"id": "worker-01", "status": "deployed"},
            "warnings": []
        },
        "agy": {
            "success": true,
            "result": {"id": "worker-01", "path": "/mock/agent.md", "status": "deployed"},
            "warnings": []
        }
    }
    """
    with patch.object(mock_adapter, "is_available", return_value=True), patch.object(
        mock_adapter, "_execute_subprocess", new_callable=AsyncMock
    ) as mock_exec:
        # 1st call: personaos import (success)
        # 2nd call: agent deploy (success)
        mock_exec.side_effect = [
            (0, "Imported worker-01", ""),
            (0, mock_deploy_json, ""),
        ]

        res = await mock_adapter.sync_worker_profile(
            worker_id="worker-01",
            profile_path=temp_profile_file,
            profile_version=1,
            target_providers=["openclaw", "agy"],
            dry_run=False,
            actor_id="admin-user",
        )

        assert res.overall_status == SyncStatus.SUCCESS
        assert res.dry_run is False
        assert len(res.targets) == 2
        assert res.targets["openclaw"].status == SyncStatus.SUCCESS
        assert res.targets["agy"].status == SyncStatus.SUCCESS
        assert res.audit_metadata["actor_id"] == "admin-user"


@pytest.mark.asyncio
async def test_sync_worker_live_partial_failure(temp_profile_file: Path, mock_adapter: PABSyncAdapter):
    mock_deploy_json = """
    {
        "openclaw": {
            "success": true,
            "result": {"id": "worker-01", "status": "deployed"},
            "warnings": []
        },
        "claude": {
            "success": false,
            "error": "Claude CLI not available",
            "warnings": []
        }
    }
    """
    with patch.object(mock_adapter, "is_available", return_value=True), patch.object(
        mock_adapter, "_execute_subprocess", new_callable=AsyncMock
    ) as mock_exec:
        mock_exec.side_effect = [
            (0, "Imported worker-01", ""),
            (0, mock_deploy_json, ""),
        ]

        res = await mock_adapter.sync_worker_profile(
            worker_id="worker-01",
            profile_path=temp_profile_file,
            profile_version=1,
            target_providers=["openclaw", "claude"],
        )

        assert res.overall_status == SyncStatus.PARTIAL
        assert res.targets["openclaw"].status == SyncStatus.SUCCESS
        assert res.targets["claude"].status == SyncStatus.FAILED
        assert res.targets["claude"].error == "Claude CLI not available"


@pytest.mark.asyncio
async def test_sync_worker_subprocess_timeout(temp_profile_file: Path, mock_adapter: PABSyncAdapter):
    with patch.object(mock_adapter, "is_available", return_value=True), patch.object(
        mock_adapter, "_execute_subprocess", new_callable=AsyncMock
    ) as mock_exec:
        # Import times out
        mock_exec.return_value = (-1, "", "Operation timed out after 5.0s")

        res = await mock_adapter.sync_worker_profile(
            worker_id="worker-01",
            profile_path=temp_profile_file,
            profile_version=1,
        )

        assert res.overall_status == SyncStatus.FAILED
        assert res.exit_code == -1
        assert "timed out" in res.stderr_summary.lower()


@pytest.mark.asyncio
async def test_sync_skill_success(mock_adapter: PABSyncAdapter):
    mock_skill_json = """
    {
        "agy": {"success": true, "security_audit": {"is_safe": true}},
        "codex": {"success": true}
    }
    """
    with patch.object(mock_adapter, "is_available", return_value=True), patch.object(
        mock_adapter, "_execute_subprocess", new_callable=AsyncMock
    ) as mock_exec:
        mock_exec.return_value = (0, mock_skill_json, "")

        res = await mock_adapter.sync_skill("pytest", target_providers=["agy", "codex"])

        assert res.overall_status == SyncStatus.SUCCESS
        assert len(res.targets) == 2
        assert res.targets["agy"].status == SyncStatus.SUCCESS
        assert res.targets["codex"].status == SyncStatus.SUCCESS


@pytest.mark.asyncio
async def test_get_provider_readiness(mock_adapter: PABSyncAdapter):
    mock_readiness_json = """
    {
        "openclaw": {"detected": true, "version": "1.0"},
        "agy": {"detected": true, "version": "1.4.11"}
    }
    """
    with patch.object(mock_adapter, "is_available", return_value=True), patch.object(
        mock_adapter, "_execute_subprocess", new_callable=AsyncMock
    ) as mock_exec:
        mock_exec.return_value = (0, mock_readiness_json, "")

        readiness = await mock_adapter.get_provider_readiness()
        assert readiness["available"] is True
        assert "openclaw" in readiness["providers"]
        assert readiness["providers"]["openclaw"]["detected"] is True


def test_sync_operation_result_to_dict():
    res = SyncOperationResult(
        operation_id="sync-abc-123",
        worker_id="worker-01",
        profile_version=2,
        requested_at=datetime(2026, 10, 31, 0, 0, 0),
        completed_at=datetime(2026, 10, 31, 0, 0, 1),
        overall_status=SyncStatus.SUCCESS,
        dry_run=False,
        targets={
            "openclaw": TargetSyncResult(
                provider="openclaw",
                status=SyncStatus.SUCCESS,
                action="deployed",
                details={"status": "ok"},
            )
        },
        pab_command=["pab", "agent", "deploy", "worker-01"],
        exit_code=0,
        stdout_summary="Success",
        stderr_summary="",
        audit_metadata={"user": "root"},
    )

    d = res.to_dict()
    assert d["operation_id"] == "sync-abc-123"
    assert d["worker_id"] == "worker-01"
    assert d["profile_version"] == 2
    assert d["overall_status"] == "success"
    assert d["targets"]["openclaw"]["status"] == "success"
    assert d["targets"]["openclaw"]["action"] == "deployed"


def test_validation_accepts_all_five_providers(temp_profile_file: Path, mock_adapter: PABSyncAdapter):
    """Verify validation passes for all five approved providers."""
    providers = ["openclaw", "agy", "codex", "claude", "minimax"]
    errors = mock_adapter._validate_inputs(
        worker_id="worker-qa-001",
        profile_path=temp_profile_file,
        profile_version=1,
        target_providers=providers,
    )
    assert errors == []


@pytest.mark.asyncio
async def test_sync_worker_all_five_providers_mocked(temp_profile_file: Path, mock_adapter: PABSyncAdapter):
    """Verify synchronization across all 5 providers normalizes results properly."""
    mock_deploy_json = """
    {
        "openclaw": {"success": true, "result": {"status": "deployed"}},
        "agy": {"success": true, "result": {"status": "deployed"}},
        "codex": {"success": true, "result": {"status": "deployed"}},
        "claude": {"success": true, "result": {"status": "deployed"}},
        "minimax": {"success": true, "result": {"status": "deployed"}}
    }
    """
    with patch.object(mock_adapter, "is_available", return_value=True), patch.object(
        mock_adapter, "_execute_subprocess", new_callable=AsyncMock
    ) as mock_exec:
        mock_exec.side_effect = [
            (0, "Imported worker-qa-001", ""),
            (0, mock_deploy_json, ""),
        ]

        res = await mock_adapter.sync_worker_profile(
            worker_id="worker-qa-001",
            profile_path=temp_profile_file,
            profile_version=1,
            target_providers=["openclaw", "agy", "codex", "claude", "minimax"],
        )

        assert res.overall_status == SyncStatus.SUCCESS
        assert len(res.targets) == 5
        for p in ["openclaw", "agy", "codex", "claude", "minimax"]:
            assert p in res.targets
            assert res.targets[p].status == SyncStatus.SUCCESS
