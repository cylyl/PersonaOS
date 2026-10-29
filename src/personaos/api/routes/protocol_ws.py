"""WebSocket WSS server transport for Cross-Machine Agent Execution Protocol (PMAEP).

Per ADR 0035 and docs/specs/cross-machine-agent-execution-protocol.md.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from personaos.db.models import TaskCheckpoint
from personaos.events.store import emit_task_event
from personaos.protocol.errors import (
    PMAEPError,
    StaleGenerationProtocolError,
)
from personaos.protocol.models import (
    METHOD_AGENT_CHECKPOINT,
    METHOD_AGENT_COMPLETE,
    METHOD_AGENT_HANDSHAKE,
    METHOD_AGENT_HEARTBEAT,
    RPCRequest,
    RPCResponse,
)
from personaos.protocol.tokens import TaskTokenManager

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/cluster", tags=["protocol"])


class ClusterWebSocketManager:
    """Manages active remote worker daemon WebSocket sessions."""

    def __init__(self, token_manager: TaskTokenManager | None = None) -> None:
        self.token_manager = token_manager or TaskTokenManager()
        self.active_sessions: dict[str, WebSocket] = {}

    async def handle_connection(
        self,
        websocket: WebSocket,
        session_factory: async_sessionmaker[AsyncSession] | None,
    ) -> None:
        await websocket.accept()
        session_id = f"sess-{uuid.uuid4().hex[:8]}"
        node_id: str | None = None

        try:
            while True:
                raw_text = await websocket.receive_text()
                try:
                    data = json.loads(raw_text)
                    rpc_req = RPCRequest.model_validate(data)
                except Exception as e:
                    err_resp = RPCResponse.fail(
                        request_id=None,
                        code=-32700,
                        message=f"Parse error: {e}",
                    )
                    await websocket.send_text(err_resp.model_dump_json())
                    continue

                response = await self._dispatch_rpc(
                    rpc_req, session_id, node_id, session_factory
                )
                if rpc_req.method == METHOD_AGENT_HANDSHAKE and not response.error:
                    node_id = rpc_req.params.get("node_id")
                    if node_id:
                        self.active_sessions[node_id] = websocket

                await websocket.send_text(response.model_dump_json())

        except WebSocketDisconnect:
            logger.info("Worker daemon disconnected", session_id=session_id, node_id=node_id)
        finally:
            if node_id:
                self.active_sessions.pop(node_id, None)

    async def _dispatch_rpc(
        self,
        req: RPCRequest,
        session_id: str,
        current_node_id: str | None,
        session_factory: async_sessionmaker[AsyncSession] | None,
    ) -> RPCResponse:
        method = req.method
        params = req.params

        if method == METHOD_AGENT_HANDSHAKE:
            node_id = params.get("node_id", "unknown-node")
            logger.info("Handling agent.handshake", node_id=node_id, session_id=session_id)
            return RPCResponse.success(
                req.id,
                result={
                    "session_id": session_id,
                    "heartbeat_interval_sec": 5.0,
                    "lease_ttl_sec": 30.0,
                    "status": "ready",
                },
                meta=req.meta,
            )

        if method == METHOD_AGENT_HEARTBEAT:
            return RPCResponse.success(
                req.id,
                result={"status": "acknowledged", "acknowledged_at": datetime.now(UTC).isoformat()},
                meta=req.meta,
            )

        if method == METHOD_AGENT_CHECKPOINT:
            if session_factory is None:
                return RPCResponse.fail(req.id, -32000, "Database session factory not configured")

            token = params.get("auth_token", "")
            task_id = params.get("task_id", "")
            generation = params.get("generation", 1)
            step_name = params.get("step_name", "")
            state = params.get("state", {})

            # Verify token
            try:
                self.token_manager.verify_token(
                    token,
                    expected_task_id=task_id,
                    expected_generation=generation,
                )
            except PMAEPError as e:
                return RPCResponse.fail(req.id, e.code, e.message, meta=req.meta)

            # Persist checkpoint to database
            chk_ref = f"chk-{uuid.uuid4().hex[:12]}"
            async with session_factory() as session:
                # 1. Insert checkpoint record first (satisfying FK on tasks.checkpoint_ref)
                chk = TaskCheckpoint(
                    ref=chk_ref,
                    task_id=task_id,
                    state=state,
                    last_step=step_name,
                    created_at=datetime.now(UTC),
                )
                session.add(chk)
                await session.flush()

                # 2. Check generation fence & update tasks
                update_sql = text(
                    """
                    UPDATE tasks
                    SET checkpoint_ref = :chk_ref,
                        lock_expires_at = NOW() + INTERVAL '30 seconds'
                    WHERE id = :task_id
                      AND generation = :generation
                    RETURNING id
                    """
                )
                res = await session.execute(
                    update_sql,
                    {"task_id": task_id, "generation": generation, "chk_ref": chk_ref},
                )
                if res.first() is None:
                    await session.rollback()
                    return RPCResponse.fail(
                        req.id,
                        StaleGenerationProtocolError.code,
                        "Generation mismatch: task was reclaimed or modified",
                        meta=req.meta,
                    )

                await session.commit()

            return RPCResponse.success(
                req.id,
                result={
                    "checkpoint_ref": chk_ref,
                    "task_id": task_id,
                    "generation": generation,
                    "committed_at": datetime.now(UTC).isoformat(),
                },
                meta=req.meta,
            )

        if method == METHOD_AGENT_COMPLETE:
            if session_factory is None:
                return RPCResponse.fail(req.id, -32000, "Database session factory not configured")

            token = params.get("auth_token", "")
            task_id = params.get("task_id", "")
            generation = params.get("generation", 1)
            result_payload = params.get("result", {})

            try:
                self.token_manager.verify_token(
                    token,
                    expected_task_id=task_id,
                    expected_generation=generation,
                )
            except PMAEPError as e:
                return RPCResponse.fail(req.id, e.code, e.message, meta=req.meta)

            async with session_factory() as session:
                update_sql = text(
                    """
                    UPDATE tasks
                    SET status = 'completed',
                        result = CAST(:res AS jsonb),
                        completed_at = NOW(),
                        lock_expires_at = NULL,
                        generation = generation + 1
                    WHERE id = :task_id
                      AND generation = :generation
                    RETURNING id
                    """
                )
                res = await session.execute(
                    update_sql,
                    {
                        "task_id": task_id,
                        "generation": generation,
                        "res": json.dumps(result_payload),
                    },
                )
                if res.first() is None:
                    return RPCResponse.fail(
                        req.id,
                        StaleGenerationProtocolError.code,
                        "Generation mismatch on completion: task was reclaimed",
                        meta=req.meta,
                    )

                await emit_task_event(
                    session,
                    task_id=task_id,
                    event_type="task.completed",
                    actor_id=current_node_id or "protocol:ws",
                )
                await session.commit()

            return RPCResponse.success(
                req.id,
                result={"task_id": task_id, "status": "completed"},
                meta=req.meta,
            )

        return RPCResponse.fail(
            req.id,
            code=-32601,
            message=f"Method not found: '{method}'",
            meta=req.meta,
        )


ws_manager = ClusterWebSocketManager()


@router.websocket("/ws")
async def cluster_websocket_endpoint(websocket: WebSocket) -> None:
    """WebSocket endpoint for remote worker daemons."""
    session_factory: async_sessionmaker[AsyncSession] | None = getattr(
        websocket.app.state, "session_factory", None
    )
    await ws_manager.handle_connection(websocket, session_factory)
