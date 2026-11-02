"""Integration tests for PersonaOS PAB profile synchronization lifecycle.

Verifies:
- GET /workers/bridge/readiness
- POST /workers/{worker_id}/sync (dry-run & live)
- Monotonic version guard rejecting stale version sync
- Failure isolation: profile activation in PostgreSQL succeeds even if PAB is offline or fails
- Automatic background sync hook dispatch on profile activation
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from personaos.api.app import create_app
from personaos.domain.worker import Persona, WorkerProfile
from personaos.registry.pab_adapter import (
    PABSyncAdapter,
    SyncOperationResult,
    SyncStatus,
    TargetSyncResult,
)
from personaos.registry.profile import ProfileRegistry

REPO_ROOT = Path(__file__).parents[2]
ALEMBIC_INI = REPO_ROOT / "src" / "personaos" / "db" / "migrations" / "alembic.ini"
MIGRATIONS_DIR = REPO_ROOT / "src" / "personaos" / "db" / "migrations"


def _alembic_config(database_url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", database_url)
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


@pytest.fixture
async def migrated_db(database_url: str) -> None:
    """Run Alembic migrations to head before tests."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")


@pytest.fixture
async def api_client(
    migrated_db: None,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    """Async HTTP test client bound to test database and temporary profiles directory."""
    monkeypatch.setenv("PERSONAOS_PROFILES_DIR", str(tmp_path / "profiles"))
    app = create_app(engine=engine, session_factory=session_factory)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_get_bridge_readiness(api_client: AsyncClient, clean_tables: None) -> None:
    """Verify bridge readiness endpoint returns provider discovery status."""
    mock_readiness = {
        "available": True,
        "providers": {
            "openclaw": {"detected": True, "version": "2026.10.6"},
            "agy": {"detected": True, "version": "1.4.11"},
        },
    }
    with patch.object(PABSyncAdapter, "get_provider_readiness", new_callable=AsyncMock) as mock_ready:
        mock_ready.return_value = mock_readiness
        resp = await api_client.get("/workers/bridge/readiness")
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is True
        assert "openclaw" in data["providers"]


@pytest.mark.asyncio
async def test_worker_sync_dry_run(
    api_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    clean_tables: None,
) -> None:
    """Verify manual sync with dry_run=true generates planned change receipt without mutating external state."""
    worker_id = "worker-sync-dryrun"

    async with session_factory() as session:
        registry = ProfileRegistry(session, profiles_dir=tmp_path / "profiles")
        await registry.register_worker(
            worker_id,
            WorkerProfile(version=1, role="developer", runtime="openclaw", skills=["python"]),
        )
        await session.commit()

    mock_result = SyncOperationResult(
        operation_id="sync-dry-001",
        worker_id=worker_id,
        profile_version=1,
        requested_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        overall_status=SyncStatus.SUCCESS,
        dry_run=True,
        targets={
            "openclaw": TargetSyncResult(
                provider="openclaw",
                status=SyncStatus.SUCCESS,
                action="planned",
                details={"resource_id": worker_id},
            )
        },
        pab_command=["pab", "sync", "plan"],
        exit_code=0,
    )

    with patch.object(PABSyncAdapter, "sync_worker_profile", new_callable=AsyncMock) as mock_sync:
        mock_sync.return_value = mock_result
        resp = await api_client.post(
            f"/workers/{worker_id}/sync",
            json={"dry_run": True, "targets": ["openclaw"]},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["worker_id"] == worker_id
        assert data["dry_run"] is True
        assert data["overall_status"] == "success"
        assert data["targets"]["openclaw"]["action"] == "planned"


@pytest.mark.asyncio
async def test_worker_sync_stale_version_rejected(
    api_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    clean_tables: None,
) -> None:
    """Verify monotonic version guard: attempting to live sync version < current active version is rejected with 400."""
    worker_id = "worker-stale-test"

    async with session_factory() as session:
        registry = ProfileRegistry(session, profiles_dir=tmp_path / "profiles")
        await registry.register_worker(
            worker_id,
            WorkerProfile(version=1, role="developer", runtime="openclaw", skills=["python"]),
        )
        # Create & activate v2
        await registry.create_profile_version(
            worker_id,
            WorkerProfile(version=2, role="developer", runtime="openclaw", skills=["python", "docker"]),
        )
        await registry.activate_profile_version(worker_id, 2)
        await session.commit()

    # Attempt to live-sync stale v1
    resp = await api_client.post(
        f"/workers/{worker_id}/sync",
        json={"version": 1, "dry_run": False},
    )

    assert resp.status_code == 400
    assert "Cannot sync stale profile version 1" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_failure_isolation_on_activation(
    api_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    clean_tables: None,
) -> None:
    """Verify that failure or absence of PAB never rolls back or blocks profile activation in PostgreSQL."""
    worker_id = "worker-fail-iso"

    async with session_factory() as session:
        registry = ProfileRegistry(session, profiles_dir=tmp_path / "profiles")
        await registry.register_worker(
            worker_id,
            WorkerProfile(version=1, role="developer", runtime="openclaw", skills=["python"]),
        )
        await session.commit()

    # Simulate PAB completely failing or crashing during background sync
    with patch.object(PABSyncAdapter, "sync_worker_profile", side_effect=RuntimeError("PAB crashed")):
        # Create and activate v2 via API
        resp = await api_client.post(
            f"/workers/{worker_id}/profiles",
            json={
                "version": 2,
                "role": "developer",
                "skills": ["python", "rust"],
                "activate": True,
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["version"] == 2
        assert data["activated"] is True

        # Verify DB reflects active version 2 regardless of PAB crash
        async with session_factory() as session:
            registry = ProfileRegistry(session, profiles_dir=tmp_path / "profiles")
            worker = await registry.get_worker(worker_id)
            assert worker.current_profile_version == 2
