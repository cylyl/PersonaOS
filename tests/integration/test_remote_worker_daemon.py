"""Integration tests for Remote Worker Daemon & Heartbeat Distribution (v0.2-B).

Per ADR 0034 and docs/specs/remote-worker-daemon-heartbeat-distribution.md:
- RWD-INT-01: Remote worker daemon boots, binds node, and emits heartbeats to PostgreSQL.
- RWD-INT-02: Server-authoritative lease extension and stale generation fencing rejection.
- RWD-INT-03: Dead node eviction quarantines non-idempotent tasks into review queue.
- RWD-INT-04: Dead node eviction fast-expires idempotent tasks for immediate safe reclamation.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from personaos.db.models import Worker as WorkerModel
from personaos.db.models import WorkerNode
from personaos.execution.kernel import StaleGenerationConflictError
from personaos.orchestrator.supervisor import NodeSupervisor
from personaos.worker_daemon.config import DaemonConfig
from personaos.worker_daemon.daemon import WorkerDaemon
from personaos.worker_daemon.heartbeat import HeartbeatAgent
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
async def test_worker_daemon_lifecycle(
    database_url: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """RWD-INT-01: Verifies worker daemon registration, node binding, and clean drain unbinding."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        # Pre-seed worker record
        worker = WorkerModel(
            id="worker-remote-1",
            name="Remote Worker 1",
            role="devops",
            status="idle",
        )
        session.add(worker)
        await session.commit()

    config = DaemonConfig(
        worker_id="worker-remote-1",
        node_id="node-remote-box-1",
        heartbeat_interval=0.2,
        heartbeat_jitter_max=0.05,
    )
    daemon = WorkerDaemon(config, session_factory=session_factory)
    await daemon.start()

    assert daemon.status == "ready"

    # Verify binding created in DB
    async with session_factory() as session:
        row = (
            await session.execute(
                text("SELECT id, unbound_at FROM worker_nodes WHERE id = 'bind-node-remote-box-1'")
            )
        ).first()
        assert row is not None
        assert row[1] is None  # Active binding

    # Let heartbeat run briefly
    await asyncio.sleep(0.5)

    # Drain and stop daemon
    await daemon.drain(reason="runner_shutdown")
    assert daemon.status == "offline"

    # Verify node marked unbound with runner_shutdown
    async with session_factory() as session:
        row = (
            await session.execute(
                text("SELECT unbound_at, reason FROM worker_nodes WHERE id = 'bind-node-remote-box-1'")
            )
        ).first()
        assert row is not None
        assert row[0] is not None
        assert row[1] == "runner_shutdown"


@pytest.mark.asyncio
async def test_heartbeat_agent_fencing_rejection_triggers_abort(
    database_url: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """RWD-INT-02: Lease renewal with stale generation raises StaleGenerationConflictError and triggers abort."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    worker_id = "worker-fenced-test"
    node_id = "node-fenced-1"
    task_id = "task-fenced-101"

    async with session_factory() as session:
        worker = WorkerModel(id=worker_id, name="Worker", role="devops")
        session.add(worker)
        await session.commit()

        wl = WorkloadService(session)
        task = await wl.enqueue(
            worker_id=worker_id,
            title="Fenced Task",
            objective="Test fencing",
            input={},
        )
        # Advance task to generation 2 in DB (simulating control plane reclaim on another node)
        await session.execute(
            text(
                """
                UPDATE tasks
                SET status = 'in_progress',
                    assigned_node = 'node-alternate',
                    generation = 2
                WHERE id = :t_id
                """
            ),
            {"t_id": task.id},
        )
        await session.commit()

    fenced_events: list[tuple[str, int]] = []

    def on_stale(t_id: str, gen: int) -> None:
        fenced_events.append((t_id, gen))

    agent = HeartbeatAgent(
        node_id=node_id,
        worker_id=worker_id,
        session_factory=session_factory,
        lease_ttl_seconds=30.0,
        on_stale_generation=on_stale,
    )
    # Node 1 believes it holds generation 1
    agent.track_task(task.id, generation=1)

    with pytest.raises(StaleGenerationConflictError):
        await agent.ping_once()

    assert len(fenced_events) == 1
    assert fenced_events[0] == (task.id, 1)


@pytest.mark.asyncio
async def test_supervisor_eviction_quarantines_non_idempotent_task(
    database_url: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """RWD-INT-03: Dead node eviction routes stranded non-idempotent tasks into review queue."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    worker_id = "worker-quarantine-target"
    dead_node = "node-crashed-non-idempotent"

    async with session_factory() as session:
        worker = WorkerModel(id=worker_id, name="Worker", role="devops")
        session.add(worker)
        await session.commit()

        wl = WorkloadService(session)
        task = await wl.enqueue(
            worker_id=worker_id,
            title="Non-Idempotent Payment Action",
            objective="Process transaction",
            input={"amount": 100},
        )
        # Mark in-progress with HAS_SIDE_EFFECTS on dead_node
        await session.execute(
            text(
                """
                UPDATE tasks
                SET status = 'in_progress',
                    assigned_node = :node_id,
                    side_effect_classification = 'HAS_SIDE_EFFECTS',
                    lock_expires_at = NOW() + INTERVAL '10 minutes'
                WHERE id = :t_id
                """
            ),
            {"node_id": dead_node, "t_id": task.id},
        )
        # Bind dead node with expired heartbeat (120s ago)
        stale_time = datetime.now(UTC) - timedelta(seconds=120)
        session.add(
            WorkerNode(
                id="bind-dead-non-idempotent",
                worker_id=worker_id,
                node_id=dead_node,
                bound_at=stale_time,
            )
        )
        await session.commit()

    supervisor = NodeSupervisor(
        session_factory=session_factory,
        default_heartbeat_timeout_seconds=30.0,
    )
    cycle = await supervisor.evict_dead_nodes(session=None)

    assert cycle.dead_nodes_evicted == 1
    assert cycle.tasks_quarantined == 1
    assert task.id in cycle.quarantined_task_ids

    # Verify task in DB transitioned to 'review' with unhandled_runtime_error
    async with session_factory() as session:
        row = (
            await session.execute(
                text("SELECT status, exhaustion_reason, lock_expires_at FROM tasks WHERE id = :t_id"),
                {"t_id": task.id},
            )
        ).first()
        assert row is not None
        assert row[0] == "review"
        assert row[1] == "unhandled_runtime_error"
        assert row[2] is None  # Lock cleared


@pytest.mark.asyncio
async def test_supervisor_eviction_fast_expires_idempotent_task(
    database_url: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """RWD-INT-04: Dead node eviction fast-expires stranded idempotent tasks for immediate safe reclamation."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    worker_id = "worker-idempotent-target"
    dead_node = "node-crashed-idempotent"

    async with session_factory() as session:
        worker = WorkerModel(id=worker_id, name="Worker", role="devops")
        session.add(worker)
        await session.commit()

        wl = WorkloadService(session)
        task = await wl.enqueue(
            worker_id=worker_id,
            title="Idempotent Compile Action",
            objective="Compile source",
            input={},
        )
        # Mark in-progress with IDEMPOTENT on dead_node
        await session.execute(
            text(
                """
                UPDATE tasks
                SET status = 'in_progress',
                    assigned_node = :node_id,
                    side_effect_classification = 'IDEMPOTENT',
                    lock_expires_at = NOW() + INTERVAL '10 minutes'
                WHERE id = :t_id
                """
            ),
            {"node_id": dead_node, "t_id": task.id},
        )
        stale_time = datetime.now(UTC) - timedelta(seconds=120)
        session.add(
            WorkerNode(
                id="bind-dead-idempotent",
                worker_id=worker_id,
                node_id=dead_node,
                bound_at=stale_time,
            )
        )
        await session.commit()

    supervisor = NodeSupervisor(
        session_factory=session_factory,
        default_heartbeat_timeout_seconds=30.0,
    )
    cycle = await supervisor.evict_dead_nodes(session=None)

    assert cycle.dead_nodes_evicted == 1
    assert cycle.tasks_expired == 1
    assert task.id in cycle.expired_task_ids

    # Verify task in DB remains in_progress but lock_expires_at is now in the past
    async with session_factory() as session:
        row = (
            await session.execute(
                text("SELECT status, lock_expires_at < NOW() FROM tasks WHERE id = :t_id"),
                {"t_id": task.id},
            )
        ).first()
        assert row is not None
        assert row[0] == "in_progress"
        assert row[1] is True  # Lock expired in the past, ready for immediate claim
