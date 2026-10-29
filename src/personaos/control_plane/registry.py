"""Control-plane cluster registry and heartbeat tracking.

Per ADR 0033 — Multi-Node Control-Plane Distribution:
Maintains active-active cluster membership in the `control_plane_nodes` table.
"""

from __future__ import annotations

import socket
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from personaos.db.models import ControlPlaneNode

logger = structlog.get_logger(__name__)


def _get_rowcount(result: Any) -> int:
    if isinstance(result, CursorResult):
        return int(result.rowcount)
    return int(getattr(result, "rowcount", 0) or 0)


@dataclass(frozen=True)
class ControlPlaneNodeInfo:
    """Read-only view of a registered control-plane node."""

    id: str
    hostname: str
    ip_address: str
    port: int
    api_endpoint: str
    status: str
    version: str
    last_heartbeat_at: datetime
    started_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


class ControlPlaneRegistry:
    """Manages active-active control-plane registration, heartbeats, and cluster membership."""

    def __init__(
        self,
        node_id: str | None = None,
        hostname: str | None = None,
        ip_address: str = "127.0.0.1",
        port: int = 8000,
        api_endpoint: str | None = None,
        version: str = "0.2.0",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.hostname = hostname or socket.gethostname()
        self.node_id = node_id or f"cp-{self.hostname}-{uuid.uuid4().hex[:8]}"
        self.ip_address = ip_address
        self.port = port
        self.api_endpoint = api_endpoint or f"http://{self.ip_address}:{self.port}"
        self.version = version
        self.metadata = metadata or {}
        self._registered = False

    async def register(self, session: AsyncSession) -> ControlPlaneNodeInfo:
        """Registers this control-plane node as active in PostgreSQL."""
        logger.info(
            "Registering control plane node",
            node_id=self.node_id,
            api_endpoint=self.api_endpoint,
        )
        existing = await session.get(ControlPlaneNode, self.node_id)
        if existing:
            existing.hostname = self.hostname
            existing.ip_address = self.ip_address
            existing.port = self.port
            existing.api_endpoint = self.api_endpoint
            existing.status = "active"
            existing.version = self.version
            existing.last_heartbeat_at = datetime.now(UTC)
            existing.node_metadata = self.metadata
        else:
            node = ControlPlaneNode(
                id=self.node_id,
                hostname=self.hostname,
                ip_address=self.ip_address,
                port=self.port,
                api_endpoint=self.api_endpoint,
                status="active",
                version=self.version,
                last_heartbeat_at=datetime.now(UTC),
                started_at=datetime.now(UTC),
                node_metadata=self.metadata,
            )
            session.add(node)

        await session.commit()
        self._registered = True
        return await self.get_node(session, self.node_id)  # type: ignore[return-value]

    async def heartbeat(
        self, session: AsyncSession, metadata_updates: dict[str, Any] | None = None
    ) -> bool:
        """Sends a heartbeat to PostgreSQL, renewing the active lease."""
        if metadata_updates:
            self.metadata.update(metadata_updates)

        stmt = (
            update(ControlPlaneNode)
            .where(ControlPlaneNode.id == self.node_id)
            .values(
                last_heartbeat_at=datetime.now(UTC),
                node_metadata=self.metadata,
            )
        )
        result = await session.execute(stmt)
        await session.commit()
        return _get_rowcount(result) > 0

    async def drain(self, session: AsyncSession) -> bool:
        """Marks this node as draining (in preparation for shutdown)."""
        logger.info("Marking control plane node as draining", node_id=self.node_id)
        stmt = (
            update(ControlPlaneNode)
            .where(ControlPlaneNode.id == self.node_id)
            .values(
                status="draining",
                last_heartbeat_at=datetime.now(UTC),
            )
        )
        result = await session.execute(stmt)
        await session.commit()
        return _get_rowcount(result) > 0

    async def deregister(self, session: AsyncSession) -> bool:
        """Marks this node as offline."""
        logger.info("Deregistering control plane node", node_id=self.node_id)
        stmt = (
            update(ControlPlaneNode)
            .where(ControlPlaneNode.id == self.node_id)
            .values(
                status="offline",
                last_heartbeat_at=datetime.now(UTC),
            )
        )
        result = await session.execute(stmt)
        await session.commit()
        self._registered = False
        return _get_rowcount(result) > 0

    @staticmethod
    async def get_node(session: AsyncSession, node_id: str) -> ControlPlaneNodeInfo | None:
        """Fetches a control-plane node by ID."""
        node = await session.get(ControlPlaneNode, node_id)
        if not node:
            return None
        return ControlPlaneNodeInfo(
            id=node.id,
            hostname=node.hostname,
            ip_address=node.ip_address,
            port=node.port,
            api_endpoint=node.api_endpoint,
            status=node.status,
            version=node.version,
            last_heartbeat_at=node.last_heartbeat_at,
            started_at=node.started_at,
            metadata=node.node_metadata,
        )

    @staticmethod
    async def list_active_nodes(
        session: AsyncSession, stale_threshold_seconds: float = 15.0
    ) -> list[ControlPlaneNodeInfo]:
        """Lists active control-plane nodes that have heartbeated within the threshold."""
        query = (
            select(ControlPlaneNode)
            .where(
                ControlPlaneNode.status == "active",
                ControlPlaneNode.last_heartbeat_at
                >= text(f"NOW() - INTERVAL '{stale_threshold_seconds} seconds'"),
            )
            .order_by(ControlPlaneNode.last_heartbeat_at.desc())
        )
        result = await session.execute(query)
        nodes = result.scalars().all()
        return [
            ControlPlaneNodeInfo(
                id=n.id,
                hostname=n.hostname,
                ip_address=n.ip_address,
                port=n.port,
                api_endpoint=n.api_endpoint,
                status=n.status,
                version=n.version,
                last_heartbeat_at=n.last_heartbeat_at,
                started_at=n.started_at,
                metadata=n.node_metadata,
            )
            for n in nodes
        ]

    @staticmethod
    async def reap_stale_nodes(
        session: AsyncSession, stale_threshold_seconds: float = 15.0
    ) -> int:
        """Evicts stale control-plane nodes by marking them offline."""
        stmt = (
            update(ControlPlaneNode)
            .where(
                ControlPlaneNode.status == "active",
                ControlPlaneNode.last_heartbeat_at
                < text(f"NOW() - INTERVAL '{stale_threshold_seconds} seconds'"),
            )
            .values(status="offline")
        )
        result = await session.execute(stmt)
        reaped_count = _get_rowcount(result)
        if reaped_count > 0:
            logger.warn(
                "Reaped stale control plane nodes",
                count=reaped_count,
                threshold=stale_threshold_seconds,
            )
            await session.commit()
        return reaped_count
