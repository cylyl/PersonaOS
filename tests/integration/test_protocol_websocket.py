"""Integration tests for Cross-Machine Agent Execution Protocol (PMAEP) WebSockets.

Per ADR 0035 and docs/specs/cross-machine-agent-execution-protocol.md:
- PMAEP-INT-01: WebSocket handshake and session establishment.
- PMAEP-INT-02: Heartbeat emission over WebSocket.
- PMAEP-INT-03: Real-time checkpoint streaming into PostgreSQL task_checkpoints.
- PMAEP-INT-04: Stale generation rejection over the wire.
- PMAEP-INT-05: Task completion and event emission over WebSocket.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from starlette.testclient import TestClient

from personaos.api.app import create_app
from personaos.db.models import Worker as WorkerModel
from personaos.protocol.models import (
    METHOD_AGENT_CHECKPOINT,
    METHOD_AGENT_COMPLETE,
    METHOD_AGENT_HANDSHAKE,
    METHOD_AGENT_HEARTBEAT,
)
from personaos.protocol.tokens import TaskTokenManager
from personaos.workload.service import WorkloadService

REPO_ROOT = Path(__file__).parents[2]
ALEMBIC_INI = REPO_ROOT / "src" / "personaos" / "db" / "migrations" / "alembic.ini"
MIGRATIONS_DIR = REPO_ROOT / "src" / "personaos" / "db" / "migrations"

pytestmark = pytest.mark.usefixtures("clean_tables")


def _alembic_config(database_url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", database_url)
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


@pytest.mark.asyncio
async def test_websocket_protocol_lifecycle(
    database_url: str,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """PMAEP-INT-01 to 05: Tests full WebSocket protocol session with checkpoints and completions."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    worker_id = "worker-ws-test"
    node_id = "node-ws-remote-1"

    # Pre-seed worker and task in database
    async with session_factory() as session:
        worker = WorkerModel(id=worker_id, name="Worker WS", role="devops")
        session.add(worker)
        await session.commit()

        wl = WorkloadService(session)
        task = await wl.enqueue(
            worker_id=worker_id,
            title="Deploy Application WS",
            objective="Deploy to cluster",
            input={"cluster": "staging"},
        )
        # Advance task to in_progress with generation 1
        await session.execute(
            text(
                """
                UPDATE tasks
                SET status = 'in_progress',
                    assigned_node = :node_id,
                    generation = 1,
                    lock_expires_at = NOW() + INTERVAL '30 seconds'
                WHERE id = :t_id
                """
            ),
            {"node_id": node_id, "t_id": task.id},
        )
        await session.commit()

    token_mgr = TaskTokenManager()
    token = token_mgr.issue_token(
        worker_id=worker_id,
        node_id=node_id,
        task_id=task.id,
        generation=1,
    )

    app = create_app(engine=engine, session_factory=session_factory)
    client = TestClient(app)

    with client.websocket_connect("/api/v1/cluster/ws") as ws:
        # 1. agent.handshake
        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": "req-1",
                "method": METHOD_AGENT_HANDSHAKE,
                "params": {"node_id": node_id, "hostname": "worker-1.internal"},
            }
        )
        resp1 = ws.receive_json()
        assert resp1["jsonrpc"] == "2.0"
        assert resp1["id"] == "req-1"
        assert resp1["result"]["status"] == "ready"
        assert "session_id" in resp1["result"]

        # 2. agent.heartbeat
        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": "req-2",
                "method": METHOD_AGENT_HEARTBEAT,
                "params": {"node_id": node_id},
            }
        )
        resp2 = ws.receive_json()
        assert resp2["id"] == "req-2"
        assert resp2["result"]["status"] == "acknowledged"

        # 3. agent.checkpoint
        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": "req-3",
                "method": METHOD_AGENT_CHECKPOINT,
                "params": {
                    "task_id": task.id,
                    "generation": 1,
                    "step_name": "manifests_applied",
                    "auth_token": token,
                    "state": {"step": 1, "applied": True},
                },
            }
        )
        resp3 = ws.receive_json()
        assert resp3["id"] == "req-3"
        assert "error" not in resp3 or resp3["error"] is None
        assert resp3["result"]["task_id"] == task.id
        chk_ref = resp3["result"]["checkpoint_ref"]
        assert chk_ref.startswith("chk-")

        # 4. agent.checkpoint with stale generation (attempt with gen 0 or 2)
        stale_token = token_mgr.issue_token(
            worker_id=worker_id,
            node_id=node_id,
            task_id=task.id,
            generation=99,
        )
        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": "req-4",
                "method": METHOD_AGENT_CHECKPOINT,
                "params": {
                    "task_id": task.id,
                    "generation": 99,
                    "step_name": "late_step",
                    "auth_token": stale_token,
                    "state": {"step": 99},
                },
            }
        )
        resp4 = ws.receive_json()
        assert resp4["id"] == "req-4"
        assert resp4["error"] is not None
        assert resp4["error"]["code"] == -32009

        # 5. agent.complete
        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": "req-5",
                "method": METHOD_AGENT_COMPLETE,
                "params": {
                    "task_id": task.id,
                    "generation": 1,
                    "auth_token": token,
                    "result": {"deployed": True, "url": "https://staging.test"},
                },
            }
        )
        resp5 = ws.receive_json()
        assert resp5["id"] == "req-5"
        assert resp5["result"]["status"] == "completed"

    # Verify database state after WebSocket session completes
    async with session_factory() as session:
        # Check task in DB is completed
        t_row = (
            await session.execute(
                text("SELECT status, checkpoint_ref, generation FROM tasks WHERE id = :t_id"),
                {"t_id": task.id},
            )
        ).first()
        assert t_row is not None
        assert t_row[0] == "completed"
        assert t_row[1] == chk_ref
        assert t_row[2] == 2  # Generation incremented on complete

        # Check checkpoint record was inserted
        chk_row = (
            await session.execute(
                text("SELECT task_id, last_step, state FROM task_checkpoints WHERE ref = :chk_ref"),
                {"chk_ref": chk_ref},
            )
        ).first()
        assert chk_row is not None
        assert chk_row[0] == task.id
        assert chk_row[1] == "manifests_applied"
        assert chk_row[2]["applied"] is True

        # Check event emitted
        event_row = (
            await session.execute(
                text("SELECT type FROM task_events WHERE task_id = :t_id AND type = 'task.completed'"),
                {"t_id": task.id},
            )
        ).first()
        assert event_row is not None
