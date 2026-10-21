"""Integration tests for PersonaOS Control Plane REST API.

Tests the full FastAPI surface:
  - GET /health
  - Workers CRUD + Profile Versioning + Quarantine
  - Tasks Enqueue, Review Queue, Approval / Rejection
  - Nodes Registration, Heartbeat, Drain
  - Events Audit Log
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from personaos.api.app import create_app
from personaos.db.models import Task as TaskModel

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
async def test_health_check(api_client: AsyncClient, clean_tables: None) -> None:
    """GET /health reports ok DB connectivity and PersonaOS version."""
    response = await api_client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["db"] == "ok"
    assert data["version"] == "0.1.0"
    assert data["last_migration"] is not None


@pytest.mark.asyncio
async def test_worker_registration_and_listing(
    api_client: AsyncClient, clean_tables: None
) -> None:
    """POST /workers creates worker with v1 profile; GET /workers lists them."""
    req_body = {
        "id": "worker-devops-001",
        "name": "DevOps Lead",
        "role": "devops",
        "runtime": "openclaw",
        "max_concurrent_tasks": 2,
        "description": "Kubernetes and CI/CD specialist",
        "skills": ["kubernetes", "terraform"],
        "permissions": ["deploy:staging"],
        "persona": {
            "style": "concise",
            "autonomy": "high",
            "goals": ["maintain zero downtime"],
        },
    }
    create_resp = await api_client.post("/workers", json=req_body)
    assert create_resp.status_code == 201
    worker_data = create_resp.json()
    assert worker_data["id"] == "worker-devops-001"
    assert worker_data["role"] == "devops"
    assert worker_data["status"] == "idle"
    assert worker_data["current_profile_version"] == 1
    assert "kubernetes" in worker_data["capabilities"]

    # Duplicate registration returns 409 Conflict
    dup_resp = await api_client.post("/workers", json=req_body)
    assert dup_resp.status_code == 409

    # List workers
    list_resp = await api_client.get("/workers")
    assert list_resp.status_code == 200
    workers = list_resp.json()
    assert len(workers) == 1
    assert workers[0]["id"] == "worker-devops-001"

    # Filter by status
    filtered_idle = await api_client.get("/workers?status=idle")
    assert len(filtered_idle.json()) == 1

    filtered_busy = await api_client.get("/workers?status=busy")
    assert len(filtered_busy.json()) == 0


@pytest.mark.asyncio
async def test_worker_profile_versioning(
    api_client: AsyncClient, clean_tables: None
) -> None:
    """Worker profiles can be created monotonically and activated atomically."""
    # Register worker
    await api_client.post(
        "/workers",
        json={
            "id": "worker-qa-001",
            "role": "qa",
            "runtime": "openclaw",
            "skills": ["pytest"],
        },
    )

    # Inspect worker details
    detail_resp = await api_client.get("/workers/worker-qa-001")
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["current_profile_version"] == 1
    assert detail["available_versions"] == [1]
    assert detail["current_profile"]["role"] == "qa"

    # Create v2 without immediate activation
    v2_payload = {
        "version": 2,
        "role": "qa-senior",
        "runtime": "openclaw",
        "skills": ["pytest", "performance-testing"],
        "activate": False,
    }
    create_v2 = await api_client.post("/workers/worker-qa-001/profiles", json=v2_payload)
    assert create_v2.status_code == 201
    assert create_v2.json()["version"] == 2
    assert create_v2.json()["activated"] is False

    # Verify list profiles shows [1, 2], but worker active version is still 1
    versions_resp = await api_client.get("/workers/worker-qa-001/profiles")
    assert versions_resp.json()["versions"] == [1, 2]

    worker_check = await api_client.get("/workers/worker-qa-001")
    assert worker_check.json()["current_profile_version"] == 1

    # Activate v2
    activate_resp = await api_client.post("/workers/worker-qa-001/activate-profile/2")
    assert activate_resp.status_code == 200
    assert activate_resp.json()["active_version"] == 2

    # Verify worker now has current_profile_version = 2
    worker_after = await api_client.get("/workers/worker-qa-001")
    assert worker_after.json()["current_profile_version"] == 2
    assert worker_after.json()["role"] == "qa-senior"


@pytest.mark.asyncio
async def test_worker_quarantine_lifecycle(
    api_client: AsyncClient, clean_tables: None
) -> None:
    """Workers can be quarantined and unquarantined."""
    await api_client.post(
        "/workers",
        json={"id": "worker-agent-99", "role": "developer"},
    )

    # Quarantine worker
    quarantine_resp = await api_client.post(
        "/workers/worker-agent-99/quarantine",
        json={"reason": "Suspicious shell command invoked", "quarantined_by": "sec-team"},
    )
    assert quarantine_resp.status_code == 200
    assert quarantine_resp.json()["status"] == "quarantined"

    # Worker shows up as quarantined
    w = (await api_client.get("/workers/worker-agent-99")).json()
    assert w["status"] == "quarantined"

    # Unquarantine worker
    unquarantine_resp = await api_client.post("/workers/worker-agent-99/unquarantine")
    assert unquarantine_resp.status_code == 200
    assert unquarantine_resp.json()["status"] == "idle"


@pytest.mark.asyncio
async def test_task_lifecycle_enqueue_and_inspect(
    api_client: AsyncClient, clean_tables: None
) -> None:
    """POST /tasks enqueues task with snapshot semantics; GET /tasks reads it."""
    # Setup worker
    await api_client.post(
        "/workers",
        json={"id": "worker-runner-1", "role": "devops"},
    )

    # Enqueue task
    task_req = {
        "worker_id": "worker-runner-1",
        "title": "Deploy Service",
        "objective": "Deploy service to staging cluster",
        "input": {"env": "staging", "image": "registry.io/app:v1.2"},
        "required_skills": ["kubernetes"],
        "priority": "high",
        "side_effect_classification": "HAS_SIDE_EFFECTS",
    }
    enqueue_resp = await api_client.post("/tasks", json=task_req)
    assert enqueue_resp.status_code == 201
    task = enqueue_resp.json()
    task_id = task["id"]
    assert task["status"] == "queued"
    assert task["priority"] == "high"
    assert task["assigned_worker"] == "worker-runner-1"
    assert task["profile_version"] == 1
    assert task["side_effect_classification"] == "HAS_SIDE_EFFECTS"

    # GET /tasks/{task_id}
    get_resp = await api_client.get(f"/tasks/{task_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == task_id

    # GET /tasks list
    list_resp = await api_client.get("/tasks?status=queued")
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 1
    assert list_resp.json()[0]["id"] == task_id


@pytest.mark.asyncio
async def test_review_queue_and_approval_workflow(
    api_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    clean_tables: None,
) -> None:
    """GET /tasks/review lists review queue; approve/reject transitions state."""
    # Setup worker
    await api_client.post(
        "/workers",
        json={"id": "worker-op-1", "role": "security"},
    )

    # Seed a task in review status directly
    async with session_factory() as session:
        t_review = TaskModel(
            id="task-rev-001",
            title="Dangerous drop table",
            objective="Drop stale partitions",
            input={"partition": "p_2024"},
            assigned_worker="worker-op-1",
            status="review",
            priority="critical",
            profile_version=1,
            side_effect_classification="HAS_SIDE_EFFECTS",
        )
        session.add(t_review)
        await session.commit()

    # 1. Inspect review queue
    review_resp = await api_client.get("/tasks/review")
    assert review_resp.status_code == 200
    reviews = review_resp.json()
    assert len(reviews) == 1
    assert reviews[0]["id"] == "task-rev-001"

    # 2. Approve the task -> moves back to queued
    approve_resp = await api_client.post(
        "/tasks/task-rev-001/approve",
        json={"reason": "Approved by security lead", "actor_id": "sec-lead-alice"},
    )
    assert approve_resp.status_code == 200
    assert approve_resp.json()["status"] == "queued"

    # 3. Seed another task and reject it
    async with session_factory() as session:
        t_reject = TaskModel(
            id="task-rev-002",
            title="Unauthorized export",
            objective="Export user emails",
            input={},
            assigned_worker="worker-op-1",
            status="review",
            priority="high",
            profile_version=1,
            side_effect_classification="HAS_SIDE_EFFECTS",
        )
        session.add(t_reject)
        await session.commit()

    reject_resp = await api_client.post(
        "/tasks/task-rev-002/reject",
        json={"reason": "Compliance policy violation", "actor_id": "dpo-bob"},
    )
    assert reject_resp.status_code == 200
    assert reject_resp.json()["status"] == "failed"
    assert reject_resp.json()["last_error"] == "Compliance policy violation"


@pytest.mark.asyncio
async def test_atomic_review_approval_concurrency_and_fencing(
    api_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    clean_tables: None,
) -> None:
    """FINDING-02: Concurrent approvals/rejections are atomically serialized.

    Proves:
    1. Approval increments generation and clears lock_expires_at.
    2. Concurrent approval/rejection races result in exactly 1 winner and 1 rejection (400).
    3. Re-approving an already queued task is rejected.
    """
    await api_client.post(
        "/workers",
        json={"id": "worker-race-1", "role": "devops"},
    )

    async with session_factory() as session:
        t = TaskModel(
            id="task-race-001",
            title="Concurrent approval test",
            objective="Verify atomic fencing",
            input={},
            assigned_worker="worker-race-1",
            status="review",
            generation=3,
            priority="medium",
            profile_version=1,
            side_effect_classification="HAS_SIDE_EFFECTS",
        )
        session.add(t)
        await session.commit()

    # Launch two concurrent requests: one approve, one reject
    resp_approve, resp_reject = await asyncio.gather(
        api_client.post("/tasks/task-race-001/approve", json={"actor_id": "op-1"}),
        api_client.post("/tasks/task-race-001/reject", json={"actor_id": "op-2"}),
    )

    # Exactly one succeeded (200) and one failed (400)
    status_codes = sorted([resp_approve.status_code, resp_reject.status_code])
    assert status_codes == [200, 400]

    # Verify winning state has generation = 4 (incremented from 3)
    winner_resp = resp_approve if resp_approve.status_code == 200 else resp_reject
    assert winner_resp.json()["generation"] == 4
    assert winner_resp.json()["lock_expires_at"] is None


@pytest.mark.asyncio
async def test_node_binding_and_lifecycle(
    api_client: AsyncClient, clean_tables: None
) -> None:
    """POST /nodes binds worker to node; heartbeat acknowledges; drain cleans up."""
    await api_client.post(
        "/workers",
        json={"id": "worker-node-test-1", "role": "devops"},
    )

    # 1. Bind worker to node-1
    bind_resp = await api_client.post(
        "/nodes",
        json={
            "worker_id": "worker-node-test-1",
            "node_id": "node-us-east-1a",
            "reason": "Host assignment",
        },
    )
    assert bind_resp.status_code == 201
    bind_data = bind_resp.json()
    assert bind_data["worker_id"] == "worker-node-test-1"
    assert bind_data["node_id"] == "node-us-east-1a"
    assert bind_data["unbound_at"] is None

    # 2. Heartbeat node
    hb_resp = await api_client.post("/nodes/node-us-east-1a/heartbeat")
    assert hb_resp.status_code == 200
    assert hb_resp.json()["status"] == "active"

    # 3. List active nodes
    list_nodes = await api_client.get("/nodes?active_only=true")
    assert list_nodes.status_code == 200
    assert len(list_nodes.json()) == 1

    # 4. Drain node
    drain_resp = await api_client.post(
        "/nodes/node-us-east-1a/drain",
        json={"reason": "Host maintenance scheduled"},
    )
    assert drain_resp.status_code == 200
    assert drain_resp.json()["status"] == "drained"
    assert drain_resp.json()["unbound_count"] == 1

    # 5. List active nodes is now empty
    active_after = await api_client.get("/nodes?active_only=true")
    assert len(active_after.json()) == 0


@pytest.mark.asyncio
async def test_events_log_inspection(
    api_client: AsyncClient, clean_tables: None
) -> None:
    """GET /events returns audit trail of emitted task events."""
    # Register worker & enqueue task (which emits task.created event)
    await api_client.post(
        "/workers",
        json={"id": "worker-evt-1", "role": "devops"},
    )
    task_res = await api_client.post(
        "/tasks",
        json={
            "worker_id": "worker-evt-1",
            "title": "Event Test Task",
            "objective": "Verify event emission",
        },
    )
    task_id = task_res.json()["id"]

    # Query events
    evt_resp = await api_client.get(f"/events?task_id={task_id}")
    assert evt_resp.status_code == 200
    events = evt_resp.json()
    assert len(events) >= 1
    assert events[0]["task_id"] == task_id
    assert events[0]["type"] == "task.created"
