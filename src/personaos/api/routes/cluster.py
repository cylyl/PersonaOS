"""Cluster membership routes for multi-node control planes.

Per ADR 0033 — Multi-Node Control-Plane Distribution:
Exposes active control-plane nodes and cluster health status.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from personaos.api.deps import SessionDep
from personaos.control_plane.registry import ControlPlaneRegistry

router = APIRouter(prefix="/api/v1/cluster", tags=["cluster"])


class ControlPlaneNodeResponse(BaseModel):
    id: str
    hostname: str
    ip_address: str
    port: int
    api_endpoint: str
    status: str
    version: str
    last_heartbeat_at: str
    started_at: str
    metadata: dict[str, Any]


class ClusterStatusResponse(BaseModel):
    cluster_size: int
    active_nodes: list[ControlPlaneNodeResponse]


@router.get("/nodes", response_model=ClusterStatusResponse)
async def list_cluster_nodes(
    session: SessionDep,
    stale_threshold_seconds: float = 15.0,
) -> ClusterStatusResponse:
    """Lists all active control-plane nodes in the cluster."""
    nodes = await ControlPlaneRegistry.list_active_nodes(
        session, stale_threshold_seconds=stale_threshold_seconds
    )
    return ClusterStatusResponse(
        cluster_size=len(nodes),
        active_nodes=[
            ControlPlaneNodeResponse(
                id=n.id,
                hostname=n.hostname,
                ip_address=n.ip_address,
                port=n.port,
                api_endpoint=n.api_endpoint,
                status=n.status,
                version=n.version,
                last_heartbeat_at=n.last_heartbeat_at.isoformat(),
                started_at=n.started_at.isoformat(),
                metadata=n.metadata,
            )
            for n in nodes
        ],
    )


@router.get("/nodes/{node_id}", response_model=ControlPlaneNodeResponse)
async def get_cluster_node(
    node_id: str,
    session: SessionDep,
) -> ControlPlaneNodeResponse:
    """Fetches details for a specific control-plane node."""
    node = await ControlPlaneRegistry.get_node(session, node_id)
    if not node:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Control plane node '{node_id}' not found",
        )
    return ControlPlaneNodeResponse(
        id=node.id,
        hostname=node.hostname,
        ip_address=node.ip_address,
        port=node.port,
        api_endpoint=node.api_endpoint,
        status=node.status,
        version=node.version,
        last_heartbeat_at=node.last_heartbeat_at.isoformat(),
        started_at=node.started_at.isoformat(),
        metadata=node.metadata,
    )


@router.post("/nodes/{node_id}/drain")
async def drain_cluster_node(
    node_id: str,
    session: SessionDep,
) -> dict[str, str]:
    """Marks a control-plane node as draining."""
    node = await ControlPlaneRegistry.get_node(session, node_id)
    if not node:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Control plane node '{node_id}' not found",
        )
    registry = ControlPlaneRegistry(node_id=node.id)
    drained = await registry.drain(session)
    if not drained:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to drain control plane node '{node_id}'",
        )
    return {"status": "draining", "node_id": node_id}
