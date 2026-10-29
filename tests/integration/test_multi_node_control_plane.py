"""Integration tests for Multi-Node Control-Plane Distribution (v0.2-A).

Per ADR 0033 and docs/specs/multi-node-control-plane-distribution.md:
- CP-INT-01: Control-plane node registration, heartbeats, and cluster listing.
- CP-INT-02: Stale control-plane node detection and reaping.
- CP-INT-03: Transactional advisory locks ensure singleton sweep exclusivity.
- CP-INT-04: Active-active concurrent task claims using FOR UPDATE SKIP LOCKED.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from personaos.api.app import create_app
from personaos.control_plane.advisory_lock import (
    LOCK_ID_ORPHAN_REAPER,
    advisory_xact_lock,
    try_advisory_xact_lock,
)
from personaos.control_plane.registry import ControlPlaneRegistry
from personaos.db.models import Worker as WorkerModel
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
async def test_control_plane_node_lifecycle(
    database_url: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """CP-INT-01: Verifies node registration, heartbeat renewal, and graceful draining."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        reg1 = ControlPlaneRegistry(
            node_id="cp-node-alpha",
            hostname="host-alpha",
            ip_address="10.0.1.1",
            port=8000,
            metadata={"az": "us-east-1a"},
        )
        info1 = await reg1.register(session)
        assert info1.id == "cp-node-alpha"
        assert info1.status == "active"
        assert info1.metadata["az"] == "us-east-1a"

        reg2 = ControlPlaneRegistry(
            node_id="cp-node-beta",
            hostname="host-beta",
            ip_address="10.0.1.2",
            port=8000,
            metadata={"az": "us-east-1b"},
        )
        await reg2.register(session)

        # List active nodes
        active_nodes = await ControlPlaneRegistry.list_active_nodes(session)
        node_ids = {n.id for n in active_nodes}
        assert "cp-node-alpha" in node_ids
        assert "cp-node-beta" in node_ids

        # Heartbeat reg1
        hb_ok = await reg1.heartbeat(session, metadata_updates={"load": 0.42})
        assert hb_ok is True
        node1_fetched = await ControlPlaneRegistry.get_node(session, "cp-node-alpha")
        assert node1_fetched is not None
        assert node1_fetched.metadata.get("load") == 0.42

        # Drain reg2
        drain_ok = await reg2.drain(session)
        assert drain_ok is True
        node2_fetched = await ControlPlaneRegistry.get_node(session, "cp-node-beta")
        assert node2_fetched is not None
        assert node2_fetched.status == "draining"

        # Deregister reg1
        dereg_ok = await reg1.deregister(session)
        assert dereg_ok is True
        node1_after = await ControlPlaneRegistry.get_node(session, "cp-node-alpha")
        assert node1_after is not None
        assert node1_after.status == "offline"


@pytest.mark.asyncio
async def test_control_plane_stale_node_reaping(
    database_url: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """CP-INT-02: Verifies stale control-plane nodes with expired heartbeats are marked offline."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        # Insert a healthy node
        reg_healthy = ControlPlaneRegistry(node_id="cp-healthy", hostname="h1")
        await reg_healthy.register(session)

        # Insert a stale node whose last heartbeat was 60s ago
        stale_time = datetime.now(UTC) - timedelta(seconds=60)
        await session.execute(
            text(
                "INSERT INTO control_plane_nodes (id, hostname, ip_address, port, api_endpoint, version, status, last_heartbeat_at) "
                "VALUES ('cp-stale', 'h2', '127.0.0.1', 8000, 'http://127.0.0.1:8000', '0.2.0', 'active', :stale_time)"
            ),
            {"stale_time": stale_time},
        )
        await session.commit()

        # Reap with 15s threshold
        reaped = await ControlPlaneRegistry.reap_stale_nodes(session, stale_threshold_seconds=15.0)
        assert reaped == 1

        stale_fetched = await ControlPlaneRegistry.get_node(session, "cp-stale")
        assert stale_fetched is not None
        assert stale_fetched.status == "offline"

        healthy_fetched = await ControlPlaneRegistry.get_node(session, "cp-healthy")
        assert healthy_fetched is not None
        assert healthy_fetched.status == "active"


@pytest.mark.asyncio
async def test_advisory_locks_mutual_exclusivity(
    database_url: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """CP-INT-03: Verifies PostgreSQL transactional advisory locks guarantee mutual exclusivity."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as s1:
        async with s1.begin():
            lock1 = await try_advisory_xact_lock(s1, LOCK_ID_ORPHAN_REAPER)
            assert lock1 is True

            # In a concurrent transaction/session, acquiring the same advisory lock fails immediately
            async with session_factory() as s2:
                async with s2.begin():
                    lock2 = await try_advisory_xact_lock(s2, LOCK_ID_ORPHAN_REAPER)
                    assert lock2 is False

        # Transaction 1 committed / finished; now transaction 2 can acquire it
        async with session_factory() as s3:
            async with s3.begin():
                lock3 = await try_advisory_xact_lock(s3, LOCK_ID_ORPHAN_REAPER)
                assert lock3 is True


@pytest.mark.asyncio
async def test_active_active_skip_locked_concurrent_claims(
    database_url: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """CP-INT-04: 5 concurrent workers claiming against 20 queued tasks process all tasks with zero duplicate claims."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    # Seed a worker and 20 tasks
    async with session_factory() as session:
        worker = WorkerModel(
            id="worker-devops-pool",
            name="DevOps Pool",
            role="devops",
            status="idle",
            max_concurrent_tasks=25,
        )
        session.add(worker)
        await session.commit()

        wl = WorkloadService(session)
        for i in range(20):
            await wl.enqueue(
                worker_id="worker-devops-pool",
                title=f"Parallel Task {i}",
                objective=f"Execute task {i}",
                input={"index": i},
                priority="medium",
            )

    claimed_task_ids: list[str] = []
    lock = asyncio.Lock()

    async def worker_claim_loop(worker_idx: int) -> None:
        cp_id = f"cp-node-{worker_idx % 2}"
        for _ in range(10):
            async with session_factory() as s:
                service = WorkloadService(s)
                task = await service.claim_atomic(
                    worker_id="worker-devops-pool",
                    node_id=f"node-{worker_idx}",
                    cp_id=cp_id,
                )
                if task:
                    async with lock:
                        claimed_task_ids.append(task.id)
                else:
                    await asyncio.sleep(0.01)

    # Launch 5 concurrent claiming coroutines across 2 simulated CP nodes
    tasks = [asyncio.create_task(worker_claim_loop(i)) for i in range(5)]
    await asyncio.gather(*tasks)

    # Assert exactly 20 tasks claimed, with ZERO duplicates
    assert len(claimed_task_ids) == 20
    assert len(set(claimed_task_ids)) == 20, "Duplicate task claims detected across concurrent schedulers!"

    # Verify all tasks in DB have status in_progress and claimed_by_cp_id populated
    async with session_factory() as session:
        rows = (
            await session.execute(
                text("SELECT id, status, generation, claimed_by_cp_id FROM tasks WHERE status = 'in_progress'")
            )
        ).all()
        assert len(rows) == 20
        for r in rows:
            assert r[1] == "in_progress"
            assert r[2] == 1
            assert r[3] in ("cp-node-0", "cp-node-1")
