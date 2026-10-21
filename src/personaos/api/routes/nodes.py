"""Node routes — worker-node binding, heartbeat, and drain."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult

from personaos.api.deps import SessionDep
from personaos.api.schemas import (
    NodeBindRequest,
    NodeDrainRequest,
    NodeResponse,
)
from personaos.db.models import Worker as WorkerModel
from personaos.db.models import WorkerNode

router = APIRouter(prefix="/nodes", tags=["nodes"])


@router.get("", response_model=list[NodeResponse])
async def list_nodes(
    session: SessionDep,
    worker_id: Annotated[str | None, Query()] = None,
    node_id: Annotated[str | None, Query()] = None,
    active_only: Annotated[bool, Query()] = True,
) -> list[WorkerNode]:
    """List worker-node bindings, optionally filtered by active status."""
    query = select(WorkerNode)
    if worker_id:
        query = query.where(WorkerNode.worker_id == worker_id)
    if node_id:
        query = query.where(WorkerNode.node_id == node_id)
    if active_only:
        query = query.where(WorkerNode.unbound_at.is_(None))

    query = query.order_by(WorkerNode.bound_at.desc())
    result = await session.execute(query)
    return list(result.scalars().all())


@router.post("", response_model=NodeResponse, status_code=status.HTTP_201_CREATED)
async def bind_worker_node(
    req: NodeBindRequest,
    session: SessionDep,
) -> WorkerNode:
    """Bind a worker to an execution node."""
    w_res = await session.execute(select(WorkerModel.id).where(WorkerModel.id == req.worker_id))
    if w_res.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Worker {req.worker_id} not found",
        )

    binding = WorkerNode(
        id=str(uuid.uuid4()),
        worker_id=req.worker_id,
        node_id=req.node_id,
        reason=req.reason,
    )
    session.add(binding)
    await session.flush()
    return binding


@router.post("/{node_id}/drain")
async def drain_node(
    node_id: str,
    session: SessionDep,
    req: NodeDrainRequest | None = None,
) -> dict[str, Any]:
    """Drain all active worker bindings on a node."""
    now = datetime.now(UTC)
    reason = (req and req.reason) or "Node drain requested"

    res = await session.execute(
        update(WorkerNode)
        .where(WorkerNode.node_id == node_id, WorkerNode.unbound_at.is_(None))
        .values(unbound_at=now, reason=reason)
    )
    rowcount = res.rowcount if isinstance(res, CursorResult) else 0
    return {
        "node_id": node_id,
        "status": "drained",
        "unbound_count": rowcount,
        "drained_at": now.isoformat(),
    }


@router.post("/{node_id}/heartbeat")
async def node_heartbeat(
    node_id: str,
    session: SessionDep,
) -> dict[str, Any]:
    """Acknowledge node liveness."""
    now = datetime.now(UTC)
    return {
        "node_id": node_id,
        "status": "active",
        "last_heartbeat_at": now.isoformat(),
    }
