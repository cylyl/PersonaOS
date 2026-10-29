"""Unit tests for Multi-Node Control-Plane Distribution (v0.2-A).

Per ADR 0033 and docs/specs/multi-node-control-plane-distribution.md.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from personaos.api.app import create_app
from personaos.control_plane.advisory_lock import (
    LOCK_ID_CP_REAPER,
    LOCK_ID_NODE_SUPERVISOR,
    LOCK_ID_ORPHAN_REAPER,
    LOCK_ID_TELEMETRY_AGGREGATOR,
)
from personaos.control_plane.registry import ControlPlaneNodeInfo, ControlPlaneRegistry


def test_control_plane_registry_node_id_format() -> None:
    """Verifies generated control plane node ID follows cp-{hostname}-{uuid[:8]}."""
    reg = ControlPlaneRegistry(hostname="prod-host-1", ip_address="192.168.1.10", port=8000)
    assert reg.node_id.startswith("cp-prod-host-1-")
    assert reg.api_endpoint == "http://192.168.1.10:8000"
    assert reg.version == "0.2.0"


def test_advisory_lock_id_constants() -> None:
    """Verifies advisory lock IDs are distinct 64-bit integers."""
    lock_ids = {
        LOCK_ID_ORPHAN_REAPER,
        LOCK_ID_NODE_SUPERVISOR,
        LOCK_ID_TELEMETRY_AGGREGATOR,
        LOCK_ID_CP_REAPER,
    }
    assert len(lock_ids) == 4
    for lid in lock_ids:
        assert isinstance(lid, int)
        assert lid > 100_000


@pytest.mark.asyncio
async def test_cluster_nodes_endpoint_empty_cluster() -> None:
    """Verifies /api/v1/cluster/nodes returns 200 with empty list when no nodes registered."""
    from unittest.mock import AsyncMock, MagicMock

    from personaos.api.deps import get_session

    app = create_app()

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_session.execute.return_value = mock_result

    async def override_get_session() -> AsyncMock:
        return mock_session

    app.dependency_overrides[get_session] = override_get_session

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/cluster/nodes")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cluster_size"] == 0
        assert data["active_nodes"] == []
