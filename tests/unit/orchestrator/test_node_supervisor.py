"""Unit tests for NodeSupervisor and worker CLI wiring (Step 21).

Validates:
- SUP-01: Stale worker heartbeats are detected as dead node bindings.
- SUP-02: Healthy worker heartbeats are not flagged as dead.
- SUP-03: Dead node eviction unbinds the worker_nodes record with heartbeat_timeout reason.
- SUP-04: Tasks in_progress on dead nodes have locks accelerated to the past.
- SUP-05: Worker CLI argument parsing for concurrency, heartbeats, and drain timeouts.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from personaos.db.models import Task as TaskModel
from personaos.db.models import Worker as WorkerModel
from personaos.db.models import WorkerNode
from personaos.domain.worker import WorkerProfile
from personaos.orchestrator.supervisor import NodeSupervisor
from personaos.registry.profile import ProfileRegistry
from personaos.worker_main import parse_args
from personaos.workload.service import WorkloadService

REPO_ROOT = Path(__file__).parents[3]
ALEMBIC_INI = REPO_ROOT / "src" / "personaos" / "db" / "migrations" / "alembic.ini"
MIGRATIONS_DIR = REPO_ROOT / "src" / "personaos" / "db" / "migrations"

pytestmark = pytest.mark.usefixtures("clean_tables")


def _alembic_config(database_url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", database_url)
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


@pytest.fixture
def profiles_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "profiles"
    monkeypatch.setenv("PERSONAOS_PROFILES_DIR", str(d))
    return d


@pytest.mark.asyncio
async def test_detect_dead_nodes_identifies_stale_heartbeats(
    database_url: str,
    session_factory: async_sessionmaker[AsyncSession],
    profiles_dir: Path,
) -> None:
    """SUP-01 & SUP-02: Identifies stale worker nodes while ignoring healthy nodes."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    now = datetime.now(UTC)
    stale_time = now - timedelta(seconds=120)
    fresh_time = now - timedelta(seconds=5)

    async with session_factory() as session:
        reg = ProfileRegistry(session=session, profiles_dir=profiles_dir)
        profile = WorkerProfile(version=1, role="devops", runtime="openclaw")
        await reg.register_worker("w-dead", profile)
        await reg.register_worker("w-alive", profile)

        # Set w-dead last_seen_at to 120s ago
        await session.execute(
            update(WorkerModel).where(WorkerModel.id == "w-dead").values(last_seen_at=stale_time)
        )
        # Set w-alive last_seen_at to 5s ago
        await session.execute(
            update(WorkerModel).where(WorkerModel.id == "w-alive").values(last_seen_at=fresh_time)
        )

        # Bind both to nodes
        session.add(
            WorkerNode(
                id="binding-dead",
                worker_id="w-dead",
                node_id="node-stale",
                bound_at=stale_time,
            )
        )
        session.add(
            WorkerNode(
                id="binding-alive",
                worker_id="w-alive",
                node_id="node-healthy",
                bound_at=now,
            )
        )
        await session.commit()

        supervisor = NodeSupervisor(session=session)
        dead = await supervisor.detect_dead_nodes(heartbeat_timeout_seconds=30.0)

        assert len(dead) == 1
        assert dead[0].worker_id == "w-dead"
        assert dead[0].node_id == "node-stale"
        assert dead[0].binding_id == "binding-dead"


@pytest.mark.asyncio
async def test_evict_dead_nodes_unbinds_and_expires_tasks(
    database_url: str,
    session_factory: async_sessionmaker[AsyncSession],
    profiles_dir: Path,
) -> None:
    """SUP-03 & SUP-04: Evicting dead nodes unbinds worker_nodes
    and fast-expires in_progress tasks.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    now = datetime.now(UTC)
    stale_time = now - timedelta(seconds=90)

    async with session_factory() as session:
        reg = ProfileRegistry(session=session, profiles_dir=profiles_dir)
        profile = WorkerProfile(version=1, role="devops", runtime="openclaw")
        await reg.register_worker("w-crashed", profile)

        await session.execute(
            update(WorkerModel).where(WorkerModel.id == "w-crashed").values(last_seen_at=stale_time)
        )

        session.add(
            WorkerNode(
                id="binding-crashed",
                worker_id="w-crashed",
                node_id="node-dead-host",
                bound_at=stale_time,
            )
        )
        await session.commit()

        # Enqueue task and set status to in_progress with active 15-minute lease on this node
        wl = WorkloadService(session=session)
        task = await wl.enqueue(
            worker_id="w-crashed",
            title="Deploy service",
            objective="Deploy to k8s",
            input={},
        )
        future_lease = now + timedelta(minutes=15)
        await session.execute(
            text(
                """
                UPDATE tasks
                SET status = 'in_progress',
                    assigned_worker = 'w-crashed',
                    assigned_node = 'node-dead-host',
                    side_effect_classification = 'IDEMPOTENT',
                    lock_expires_at = :future_lease
                WHERE id = :task_id
                """
            ),
            {"future_lease": future_lease, "task_id": task.id},
        )
        await session.commit()

        # Evict dead nodes
        supervisor = NodeSupervisor(session=session)
        res = await supervisor.evict_dead_nodes(heartbeat_timeout_seconds=30.0)

        assert res.dead_nodes_evicted == 1
        assert res.tasks_expired == 1
        assert task.id in res.expired_task_ids

        # Check worker_nodes binding was unbound
        binding = await session.get(WorkerNode, "binding-crashed")
        assert binding is not None
        assert binding.unbound_at is not None
        assert binding.reason == "heartbeat_timeout"

        # Check task lock is now in the past
        t_row = (
            await session.execute(
                select(TaskModel.lock_expires_at, TaskModel.status).where(TaskModel.id == task.id)
            )
        ).first()
        assert t_row is not None
        lock_expires_at, status = t_row
        assert status == "in_progress"
        assert lock_expires_at is not None
        assert lock_expires_at < datetime.now(UTC)


def test_cli_argument_parsing() -> None:
    """SUP-05: Validates worker_main CLI argument parsing."""
    args = parse_args(
        [
            "--worker-id",
            "worker-agent-99",
            "--node-id",
            "compute-node-42",
            "--concurrency",
            "8",
            "--heartbeat-interval",
            "12.5",
            "--drain-timeout",
            "45.0",
        ]
    )
    assert args.worker_id == "worker-agent-99"
    assert args.node_id == "compute-node-42"
    assert args.concurrency == 8
    assert args.heartbeat_interval == 12.5
    assert args.drain_timeout == 45.0

    # Default arguments
    def_args = parse_args([])
    assert def_args.concurrency is None
    assert def_args.heartbeat_interval == 30.0
    assert def_args.drain_timeout == 30.0
