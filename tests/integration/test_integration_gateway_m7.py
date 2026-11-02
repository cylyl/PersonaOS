"""Milestone M7 Integration Tests — Cross-System End-to-End Suite.

Governed by ADR 0041 (PersonaOS–Orca Integration Gateway Architecture).
Verifies end-to-end integration across:
1. Orca ADE (Plugin client & ExecutionAdapter)
2. Integration Gateway (/integration/* standardized contracts)
3. PersonaOS Core Kernel (Durable PostgreSQL task queue, generation fencing, review queue)
4. PAB Control Plane (Worker capability discovery & provider readiness)

Scenarios Tested:
- Scenario 1: Gateway health, connectivity diagnostics, and auth validation (M2)
- Scenario 2: Workforce discovery and PAB provider capabilities (M3)
- Scenario 3: Task enqueue with Orca worktree context & DAG wave projection (M4)
- Scenario 4: HITL interactive decision gates with generation fencing (M5)
- Scenario 5: Real-time SSE lifecycle event streaming and Last-Event-ID resumption (M6)
- Scenario 6: Strict architectural safety invariants (zero direct agent CLI execution)
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from personaos.api.app import create_app
from personaos.db.models import Task as TaskModel
from personaos.domain.worker import Persona, WorkerProfile
from personaos.integrations.common.contracts import WorktreeDescriptor
from personaos.integrations.common.event_bus import gateway_event_bus
from personaos.integrations.orca.client import OrcaCLIClient
from personaos.integrations.orca.plugin import (
    DirectAgentExecutionViolationError,
    PersonaOSOrcaPlugin,
    PersonaOSOrcaPluginError,
)
from personaos.registry.profile import ProfileRegistry

REPO_ROOT = Path(__file__).parents[2]
ALEMBIC_INI = REPO_ROOT / "src" / "personaos" / "db" / "migrations" / "alembic.ini"
MIGRATIONS_DIR = REPO_ROOT / "src" / "personaos" / "db" / "migrations"

pytestmark = pytest.mark.usefixtures("clean_tables")


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
def mock_worktree() -> WorktreeDescriptor:
    return WorktreeDescriptor(
        worktree_id="repo-main::/workspace/m7-test",
        instance_id="inst-m7-01",
        repo_id="repo-main",
        path=Path("/workspace/m7-test"),
        branch="refs/heads/feature/m7-gateway",
        head="f1e2d3c4b5a6",
        display_name="m7-feature-worktree",
    )


@pytest.fixture
def mock_orca_client(mock_worktree: WorktreeDescriptor) -> OrcaCLIClient:
    client = MagicMock(spec=OrcaCLIClient)
    client.is_available.return_value = True
    client.get_current_worktree = AsyncMock(return_value=mock_worktree)
    return client


@pytest.fixture
async def app_and_plugin(
    migrated_db: None,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    mock_orca_client: OrcaCLIClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[AsyncClient, PersonaOSOrcaPlugin]]:
    """Fixture providing initialized FastAPI app, AsyncClient, and PersonaOSOrcaPlugin."""
    monkeypatch.setenv("PERSONAOS_PROFILES_DIR", str(tmp_path / "profiles"))
    app = create_app(engine=engine, session_factory=session_factory)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        plugin = PersonaOSOrcaPlugin(
            api_base_url="http://test",
            orca_client=mock_orca_client,
            transport=transport,
            timeout=10.0,
        )
        yield client, plugin


async def _register_test_worker(
    session: AsyncSession, profiles_dir: Path, worker_id: str = "worker-devops-m7"
) -> None:
    """Helper to register a persistent worker and its active v1 profile."""
    registry = ProfileRegistry(session=session, profiles_dir=profiles_dir)
    await registry.register_worker(
        worker_id,
        WorkerProfile(
            version=1,
            role="devops",
            runtime="openclaw",
            max_concurrent_tasks=2,
            description="DevOps automation worker",
            skills=["docker", "pytest", "k8s"],
            permissions=["read", "write"],
            persona=Persona(style="concise"),
        ),
    )
    await session.commit()


# =============================================================================
# Scenario 1: Gateway Diagnostics & Health Probing (M2)
# =============================================================================


@pytest.mark.asyncio
async def test_m7_scenario_01_gateway_diagnostics_and_health(
    app_and_plugin: tuple[AsyncClient, PersonaOSOrcaPlugin],
) -> None:
    """Verify Orca plugin retrieves structured diagnostics and operational health from Gateway."""
    _, plugin = app_and_plugin

    # 1. Check Gateway health
    health = await plugin.get_health()
    assert health.gateway_version == "1.0.0"
    assert health.personaos_connected is True
    assert health.pab_connected is True
    assert health.status in ("healthy", "degraded")

    # 2. Check Gateway deep connection diagnostics
    diagnostics = await plugin.get_diagnostics()
    assert diagnostics.code == "CONNECTED"
    assert diagnostics.status == "ok"
    assert diagnostics.latency_ms >= 0.0
    assert diagnostics.cors_supported is True
    assert diagnostics.pna_supported is True
    assert diagnostics.api_version_match is True


# =============================================================================
# Scenario 2: Workforce Discovery & PAB Provider Bridging (M3)
# =============================================================================


@pytest.mark.asyncio
async def test_m7_scenario_02_workforce_discovery_and_pab_capabilities(
    app_and_plugin: tuple[AsyncClient, PersonaOSOrcaPlugin],
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Verify registered PersonaOS workers and PAB provider capabilities are exposed via Gateway."""
    _, plugin = app_and_plugin

    async with session_factory() as session:
        await _register_test_worker(session, tmp_path / "profiles", "worker-devops-m7")

    workers = await plugin.list_integration_workers()
    assert isinstance(workers, list)
    devops_worker = next((w for w in workers if w.id == "worker-devops-m7"), None)
    assert devops_worker is not None
    assert devops_worker.role == "devops"


# =============================================================================
# Scenario 3: Orca Worktree Context Binding & Multi-Tier DAG Projection (M4)
# =============================================================================


@pytest.mark.asyncio
async def test_m7_scenario_03_worktree_binding_and_dag_wave_projection(
    app_and_plugin: tuple[AsyncClient, PersonaOSOrcaPlugin],
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Verify Orca worktree context attaches to tasks and DAG waves project correctly."""
    _, plugin = app_and_plugin
    run_id = "run-m7-dag-001"

    async with session_factory() as session:
        await _register_test_worker(session, tmp_path / "profiles", "worker-devops-m7")

    # 1. Enqueue Task A (Wave 1 root)
    task_a = await plugin.submit_task(
        worker_id="worker-devops-m7",
        title="Setup Environment",
        objective="Prepare container environment for run",
        input_data={"run_id": run_id},
        link_active_worktree=True,
    )
    assert "id" in task_a
    assert task_a["status"] == "queued"

    # Verify worktree binding in database
    async with session_factory() as session:
        t_a = await session.get(TaskModel, task_a["id"])
        assert t_a is not None
        assert isinstance(t_a.input, dict)
        assert t_a.input.get("workspace_path") == "/workspace/m7-test"
        assert t_a.input.get("worktree", {}).get("branch") == "refs/heads/feature/m7-gateway"

    # 2. Enqueue Task B (Wave 2 dependent on Task A)
    task_b = await plugin.submit_task(
        worker_id="worker-devops-m7",
        title="Execute Integration Tests",
        objective="Run pytest test suite in worktree",
        input_data={"run_id": run_id},
        dependencies=[task_a["id"]],
        link_active_worktree=True,
    )
    assert "id" in task_b

    # 3. Create or register coordination run
    await plugin.create_run(
        run_id=run_id,
        objective="Verify M7 Gateway DAG Projection",
        coordinator_handle="se-lead",
    )

    # 4. Fetch projected DAG from Gateway
    dag = await plugin.get_run_dag(run_id)
    assert dag["run_id"] == run_id
    assert dag["total_tasks"] == 2
    assert len(dag["nodes"]) == 2

    node_a = next(n for n in dag["nodes"] if n["task_id"] == task_a["id"])
    node_b = next(n for n in dag["nodes"] if n["task_id"] == task_b["id"])
    assert node_a["wave"] == 1
    assert node_b["wave"] == 2

    # 5. Fetch Ready Waves from Gateway
    ready_waves = await plugin.get_run_ready_waves(run_id)
    assert ready_waves["run_id"] == run_id
    assert len(ready_waves["waves"]) >= 1
    wave_1_tasks = [t["task_id"] for t in ready_waves["waves"][0]["ready_tasks"]]
    assert task_a["id"] in wave_1_tasks
    assert task_b["id"] not in wave_1_tasks


# =============================================================================
# Scenario 4: HITL Decision Gate Resolution with Generation Fencing (M5)
# =============================================================================


@pytest.mark.asyncio
async def test_m7_scenario_04_decision_gates_and_generation_fencing(
    app_and_plugin: tuple[AsyncClient, PersonaOSOrcaPlugin],
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Verify decision gates reject stale generation counters and resolve cleanly."""
    _, plugin = app_and_plugin
    run_id = "run-m7-hitl-001"

    # 1. Enqueue task directly into review status
    async with session_factory() as session:
        await _register_test_worker(session, tmp_path / "profiles", "worker-devops-m7")
        review_task = TaskModel(
            id="task-hitl-m7-001",
            title="Production Deployment Gate",
            objective="Approve canary deployment to production",
            assigned_worker="worker-devops-m7",
            status="review",
            priority="high",
            type="deployment",
            generation=3,
            input={"run_id": run_id},
        )
        session.add(review_task)
        await session.commit()
        task_id = review_task.id

    # 2. List decision gates via Gateway
    gates = await plugin.list_decision_gates(run_id=run_id)
    gate = next(
        (g for g in gates if g.get("gate_id") == task_id or g.get("task_id") == task_id),
        None,
    )
    assert gate is not None
    assert gate["status"] == "pending"
    assert gate["execution_generation"] == 3

    # 3. Attempt resolution with STALE generation (expected_generation=2 != actual 3)
    with pytest.raises(PersonaOSOrcaPluginError) as exc_info:
        await plugin.resolve_decision_gate(
            gate_id=task_id,
            decision="approve",
            expected_generation=2,
            review_notes="Stale resolution attempt",
        )
    assert "409" in str(exc_info.value) or "STALE_GENERATION_CONFLICT" in str(exc_info.value)

    # 4. Resolve with MATCHING generation (expected_generation=3)
    resolution = await plugin.resolve_decision_gate(
        gate_id=task_id,
        decision="approve",
        expected_generation=3,
        review_notes="Operator approved deployment canary",
    )
    assert resolution.status in ("approved", "resolved", "completed")
    assert resolution.gate_id in (task_id, f"gate-{task_id}")

    # Verify task transitioned in database
    async with session_factory() as session:
        t_resolved = await session.get(TaskModel, task_id)
        assert t_resolved is not None
        assert t_resolved.status in ("queued", "completed", "running")


# =============================================================================
# Scenario 5: Real-Time SSE Lifecycle Streaming & Auto-Reconnection (M6)
# =============================================================================


@pytest.mark.asyncio
async def test_m7_scenario_05_sse_lifecycle_event_streaming(
    app_and_plugin: tuple[AsyncClient, PersonaOSOrcaPlugin],
) -> None:
    """Verify SSE event bus broadcasts events and plugin streams them with Last-Event-ID."""
    _, plugin = app_and_plugin
    run_id = "run-m7-sse-001"

    gateway_event_bus.clear()

    # Pre-publish initial event
    evt_1 = gateway_event_bus.publish_task_status_changed(
        task_id="t-m7-sse-001",
        status="running",
        run_id=run_id,
        event_id="evt-m7-001",
        timestamp=datetime.now(UTC),
    )
    assert evt_1.event_id == "evt-m7-001"

    # Publish wave advance event
    gateway_event_bus.publish_ready_wave_advanced(
        run_id=run_id,
        current_wave=2,
        event_id="evt-m7-002",
        timestamp=datetime.now(UTC),
    )

    # Stream events starting from Last-Event-ID evt-m7-001 (should replay evt-m7-002)
    received_events = []
    async for event in plugin.stream_events(
        run_id=run_id,
        last_event_id="evt-m7-001",
        max_events=1,
    ):
        received_events.append(event)
        break

    assert len(received_events) == 1
    assert received_events[0].event_id == "evt-m7-002"
    assert received_events[0].event_type == "ready_wave_advanced"
    assert received_events[0].payload.get("current_wave") == 2


# =============================================================================
# Scenario 6: Architectural Invariants & Safety Fences (ADR 0041)
# =============================================================================


def test_m7_scenario_06_strict_direct_agent_execution_prohibition() -> None:
    """Verify Orca plugin strictly rejects direct CLI invocations of external agent binaries."""
    plugin = PersonaOSOrcaPlugin(api_base_url="http://localhost:8000")

    prohibited_commands = [
        "claude --dangerously-skip-permissions",
        "codex exec --workspace /test",
        "agy run /path/to/task",
        "openclaw agent --prompt 'do something'",
        "/usr/local/bin/claude",
        "gemini query 'help'",
        "cursor --headless",
    ]

    for cmd in prohibited_commands:
        with pytest.raises(DirectAgentExecutionViolationError) as exc_info:
            plugin.guard_no_direct_agent_execution(cmd)
        assert "Direct CLI invocation" in str(exc_info.value)
        assert "is prohibited" in str(exc_info.value)

    # Permitted non-agent system utilities
    plugin.guard_no_direct_agent_execution("git status")
    plugin.guard_no_direct_agent_execution("python3 -m pytest")
    plugin.guard_no_direct_agent_execution("npm run build")
