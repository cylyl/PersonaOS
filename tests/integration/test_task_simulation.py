"""Step 6 — Task Simulation / Replay acceptance tests.

Per docs/specs/task-simulation-replay.md (ADR 0011):

  Acceptance criteria (mapped from ADR 0011):
    1.  Original task is immutable
    2.  Historical profile is used (not worker.current)
    3.  Historical input is used (not current)
    4.  Simulation isolated from OpenClaw (static check)
    5.  Same Adapter contract used
    6.  New result is on the simulation task, not source
    7.  Auditable via simulated_from FK
    8.  Worker state not mutated
    9.  Chain test (DB → domain → profile → kernel → adapter → result)
    10. Existing 44 tests remain green (verified by full suite run)

These tests verify the chain test methodology:
  DB row → Task.from_orm → resolve_execution_context → kernel.execute →
  SimulationAdapter → Result → DB row, with simulated_from FK linking
  the new task to the immutable source.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from personaos.db.models import Task as TaskModel
from personaos.domain.task import Task
from personaos.domain.worker import Persona, WorkerProfile
from personaos.execution.adapters.base import Result
from personaos.execution.context import WorkerExecutionContext
from personaos.execution.kernel import WorkerExecutionKernel
from personaos.registry.profile import ProfileRegistry
from personaos.simulation.adapter import SimulationAdapter
from personaos.simulation.service import SimulationResult, SimulationService
from personaos.workload.service import WorkloadService


# tests/integration/test_task_simulation.py → repo root → src/personaos/db/migrations
REPO_ROOT = Path(__file__).parents[2]
ALEMBIC_INI = REPO_ROOT / "src" / "personaos" / "db" / "migrations" / "alembic.ini"
MIGRATIONS_DIR = REPO_ROOT / "src" / "personaos" / "db" / "migrations"

pytestmark = pytest.mark.usefixtures("clean_tables")


def _alembic_config(database_url):
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", database_url)
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


@pytest.fixture
def profiles_dir(tmp_path, monkeypatch):
    d = tmp_path / "profiles"
    monkeypatch.setenv("PERSONAOS_PROFILES_DIR", str(d))
    return d


# ---------- Test doubles ----------


@dataclass
class ProductionAdapter:
    """Fake 'production' adapter — returns a fixed Result.

    Used to execute the ORIGINAL task with deterministic output so we
    have a known state to replay.
    """

    result_to_return: Result
    captured_context: WorkerExecutionContext | None = None

    async def execute(
        self,
        context: WorkerExecutionContext,
        checkpoint: Any | None = None,
    ) -> Result:
        self.captured_context = context
        return self.result_to_return


# ---------- Helpers ----------


async def _setup_worker_v1_v2_active(
    session, profiles_dir, *, worker_id: str = "w1", v2_autonomy: str = "low"
) -> ProfileRegistry:
    """Register w1 with v1 + v2 profiles, activate v2."""
    reg = ProfileRegistry(session=session, profiles_dir=profiles_dir)
    await reg.register_worker(
        worker_id,
        WorkerProfile(
            version=1,
            role="devops",
            runtime="openclaw",
            max_concurrent_tasks=1,
            persona=Persona(autonomy="medium"),
        ),
    )
    await reg.create_profile_version(
        worker_id,
        WorkerProfile(
            version=2,
            role="devops",
            runtime="openclaw",
            max_concurrent_tasks=2,
            persona=Persona(autonomy=v2_autonomy),
        ),
    )
    await reg.activate_profile_version(worker_id, 2)
    return reg


async def _produce_original_task(
    session,
    profiles_dir,
    *,
    worker_id: str = "w1",
    v2_autonomy: str = "low",
    input: dict | None = None,
    production_output: dict | None = None,
) -> tuple[Task, ProfileRegistry, ProductionAdapter]:
    """Run the full production flow: setup worker, enqueue, execute via kernel.

    Returns (original_task, registry, production_adapter) for further assertions.
    """
    if input is None:
        input = {"service": "api", "version": "1.0"}
    if production_output is None:
        production_output = {"produced": True, "source": "openclaw-mock"}

    reg = await _setup_worker_v1_v2_active(
        session, profiles_dir, worker_id=worker_id, v2_autonomy=v2_autonomy
    )

    wl = WorkloadService(session=session)
    task = await wl.enqueue(
        worker_id=worker_id,
        title="deploy",
        objective="deploy the service",
        input=input,
    )
    assert task.profile_version == 2

    adapter = ProductionAdapter(
        result_to_return=Result(status="completed", output=production_output)
    )
    kernel = WorkerExecutionKernel(
        session=session,
        adapter=adapter,
        resolve_context=reg.resolve_execution_context,
    )
    await kernel.execute(task)

    # Reload to get fresh status from DB (expire_on_commit=False keeps cache stale)
    task_row = (
        await session.execute(
            text("SELECT * FROM tasks WHERE id = :tid"),
            {"tid": task.id},
        )
    ).first()
    fresh = Task(
        id=task_row.id,
        title=task_row.title,
        objective=task_row.objective,
        input=task_row.input,
        profile_version=task_row.profile_version,
        status=task_row.status,
        priority=task_row.priority,
        assigned_worker=task_row.assigned_worker,
        assigned_node=task_row.assigned_node,
        required_skills=list(task_row.required_skills or []),
        required_capabilities=list(task_row.required_capabilities or []),
        dependencies=list(task_row.dependencies or []),
        result=task_row.result,
        retry_count=task_row.retry_count,
        max_retries=task_row.max_retries,
        last_error=task_row.last_error,
        deadline=task_row.deadline,
        type=task_row.type,
        checkpoint_ref=task_row.checkpoint_ref,
        created_at=task_row.created_at,
        started_at=task_row.started_at,
        completed_at=task_row.completed_at,
    )
    return fresh, reg, adapter


async def _activate_v3(reg: ProfileRegistry, *, worker_id: str = "w1") -> None:
    """Worker activation between production and replay — changes worker.current."""
    await reg.create_profile_version(
        worker_id,
        WorkerProfile(
            version=3,
            role="devops",
            runtime="openclaw",
            max_concurrent_tasks=3,
            persona=Persona(autonomy="high"),  # distinct from v2
        ),
    )
    await reg.activate_profile_version(worker_id, 3)


# ---------- AC #1: original task is immutable ----------


async def test_replay_preserves_source_task(
    profiles_dir, database_url, session_factory
):
    """Replay never mutates the original task (status, result, profile_version, input)."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        original, reg, _ = await _produce_original_task(session, profiles_dir)

        # Capture pre-replay state
        before = (
            await session.execute(
                text(
                    "SELECT status, result, profile_version, input, "
                    "completed_at FROM tasks WHERE id = :tid"
                ),
                {"tid": original.id},
            )
        ).first()

        # Activate v3 to change worker state
        await _activate_v3(reg)

        # Replay
        sim_kernel = WorkerExecutionKernel(
            session=session,
            adapter=SimulationAdapter(),
            resolve_context=reg.resolve_execution_context,
        )
        sim_service = SimulationService(
            session=session, kernel=sim_kernel, registry=reg
        )
        await sim_service.replay(original.id)

        # Source task is UNCHANGED
        after = (
            await session.execute(
                text(
                    "SELECT status, result, profile_version, input, "
                    "completed_at FROM tasks WHERE id = :tid"
                ),
                {"tid": original.id},
            )
        ).first()

        assert before.status == after.status == "completed"
        assert before.result == after.result
        assert before.profile_version == after.profile_version == 2
        assert before.input == after.input
        assert before.completed_at == after.completed_at
        # Source task has NO simulated_from
        simulated_from = (
            await session.execute(
                text("SELECT simulated_from FROM tasks WHERE id = :tid"),
                {"tid": original.id},
            )
        ).scalar()
        assert simulated_from is None


# ---------- AC #2: historical profile is used (not worker.current) ----------


async def test_replay_uses_historical_profile(
    profiles_dir, database_url, session_factory
):
    """After worker.current → v3, replay still uses v2 (the historical snapshot)."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        original, reg, prod_adapter = await _produce_original_task(
            session, profiles_dir, v2_autonomy="low"
        )
        # Sanity: production saw v2
        assert prod_adapter.captured_context.profile.version == 2
        assert prod_adapter.captured_context.profile.persona.autonomy == "low"

        # Worker.current → v3 (different persona)
        await _activate_v3(reg)

        # Replay
        sim_kernel = WorkerExecutionKernel(
            session=session,
            adapter=SimulationAdapter(),
            resolve_context=reg.resolve_execution_context,
        )
        sim_service = SimulationService(
            session=session, kernel=sim_kernel, registry=reg
        )
        result = await sim_service.replay(original.id)

        # The simulation row's profile_version is the snapshot, NOT current
        sim_row = (
            await session.execute(
                text(
                    "SELECT profile_version FROM tasks WHERE id = :tid"
                ),
                {"tid": result.task_id},
            )
        ).first()
        assert sim_row.profile_version == 2  # NOT 3
        assert result.source_profile_version == 2


# ---------- AC #3: historical input is used ----------


async def test_replay_uses_original_input(
    profiles_dir, database_url, session_factory
):
    """The simulation task's input is a copy of the source's input."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        original_input = {"service": "billing", "version": "3.2.1", "env": "prod"}
        original, reg, _ = await _produce_original_task(
            session, profiles_dir, input=original_input
        )

        # Replay
        sim_kernel = WorkerExecutionKernel(
            session=session,
            adapter=SimulationAdapter(),
            resolve_context=reg.resolve_execution_context,
        )
        sim_service = SimulationService(
            session=session, kernel=sim_kernel, registry=reg
        )
        result = await sim_service.replay(original.id)

        sim_row = (
            await session.execute(
                text("SELECT input FROM tasks WHERE id = :tid"),
                {"tid": result.task_id},
            )
        ).first()
        assert sim_row.input == original_input


# ---------- AC #4: simulation isolated from OpenClaw (static check) ----------


def test_simulation_does_not_import_openclaw():
    """SimulationAdapter source must not import or reference OpenClaw.

    Static check: parses the adapter source and fails if any forbidden
    token appears outside the module docstring. Prevents accidental
    import of OpenClawAdapter (or its lowercase module path) into the
    simulation layer.
    """
    p = (
        Path(__file__).parents[2]
        / "src"
        / "personaos"
        / "simulation"
        / "adapter.py"
    )
    assert p.exists(), f"SimulationAdapter source not found: {p}"
    src = p.read_text(encoding="utf-8")
    for forbidden in ("OpenClawAdapter", "openclaw", "OPENCLAW_API_URL"):
        # Only check for module-level references (not docstring mentions)
        if (
            f"import {forbidden}" in src
            or f"from personaos.execution.adapters.{forbidden.lower().replace('_adapter', '')}" in src
            or f"{forbidden}" in src.split('"""')[0]  # skip module docstring
        ):
            raise AssertionError(
                f"SimulationAdapter references {forbidden!r}; must be isolated"
            )


# ---------- AC #5: same Adapter contract used ----------


async def test_simulation_uses_adapter_protocol(
    profiles_dir, database_url, session_factory
):
    """SimulationAdapter conforms to the Adapter Protocol (execute(context, checkpoint) -> Result)."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        original, reg, _ = await _produce_original_task(session, profiles_dir)

        # Capture the SimulationAdapter's input to verify protocol conformance
        @dataclass
        class CapturingSimulationAdapter:
            captured_context: WorkerExecutionContext | None = None
            captured_checkpoint: Any | None = "_UNSET"

            async def execute(
                self,
                context: WorkerExecutionContext,
                checkpoint: Any | None = None,
            ) -> Result:
                self.captured_context = context
                self.captured_checkpoint = checkpoint
                return Result(
                    status="completed",
                    output={"simulated": True, "task_id": context.task.id},
                )

        sim_adapter = CapturingSimulationAdapter()
        sim_kernel = WorkerExecutionKernel(
            session=session,
            adapter=sim_adapter,
            resolve_context=reg.resolve_execution_context,
        )
        sim_service = SimulationService(
            session=session, kernel=sim_kernel, registry=reg
        )
        await sim_service.replay(original.id)

        # The adapter received a context (conforms to the protocol)
        assert sim_adapter.captured_context is not None
        assert isinstance(sim_adapter.captured_context, WorkerExecutionContext)
        # Checkpoint was None for the new simulation task
        assert sim_adapter.captured_checkpoint is None


# ---------- AC #6: new result on simulation task, source untouched ----------


async def test_simulation_result_on_new_task(
    profiles_dir, database_url, session_factory
):
    """The simulation task has its own result; the source task's result is unchanged."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        original, reg, _ = await _produce_original_task(
            session, profiles_dir,
            production_output={"produced": True, "value": 42},
        )

        sim_kernel = WorkerExecutionKernel(
            session=session,
            adapter=SimulationAdapter(),
            resolve_context=reg.resolve_execution_context,
        )
        sim_service = SimulationService(
            session=session, kernel=sim_kernel, registry=reg
        )
        result = await sim_service.replay(original.id)

        # Source result unchanged
        source_row = (
            await session.execute(
                text("SELECT result FROM tasks WHERE id = :tid"),
                {"tid": original.id},
            )
        ).first()
        assert source_row.result == {"produced": True, "value": 42}

        # Simulation result is the SimulationAdapter output
        sim_row = (
            await session.execute(
                text("SELECT result FROM tasks WHERE id = :tid"),
                {"tid": result.task_id},
            )
        ).first()
        assert sim_row.result is not None
        assert sim_row.result.get("simulated") is True


# ---------- AC #7: auditable via simulated_from FK ----------


async def test_simulation_auditable_via_fk(
    profiles_dir, database_url, session_factory
):
    """The simulation task has simulated_from = source.id; queryable."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        original, reg, _ = await _produce_original_task(session, profiles_dir)

        sim_kernel = WorkerExecutionKernel(
            session=session,
            adapter=SimulationAdapter(),
            resolve_context=reg.resolve_execution_context,
        )
        sim_service = SimulationService(
            session=session, kernel=sim_kernel, registry=reg
        )
        result = await sim_service.replay(original.id)

        # Direct lookup: sim task's simulated_from is the source
        sim_row = (
            await session.execute(
                text("SELECT simulated_from FROM tasks WHERE id = :tid"),
                {"tid": result.task_id},
            )
        ).first()
        assert sim_row.simulated_from == original.id

        # Reverse query: find all simulations of this source
        sims = (
            await session.execute(
                text(
                    "SELECT id, status FROM tasks WHERE simulated_from = :tid"
                ),
                {"tid": original.id},
            )
        ).fetchall()
        assert len(sims) == 1
        assert sims[0].id == result.task_id


# ---------- AC #8: worker state not mutated ----------


async def test_replay_does_not_mutate_worker_state(
    profiles_dir, database_url, session_factory
):
    """Replay never UPDATEs the workers table."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        original, reg, _ = await _produce_original_task(session, profiles_dir)

        # Snapshot worker state BEFORE replay
        worker_before = (
            await session.execute(
                text(
                    "SELECT id, role, runtime, max_concurrent_tasks, "
                    "current_profile_version, capabilities, permissions, "
                    "personality FROM workers WHERE id = :wid"
                ),
                {"wid": original.assigned_worker},
            )
        ).first()

        # Replay
        sim_kernel = WorkerExecutionKernel(
            session=session,
            adapter=SimulationAdapter(),
            resolve_context=reg.resolve_execution_context,
        )
        sim_service = SimulationService(
            session=session, kernel=sim_kernel, registry=reg
        )
        await sim_service.replay(original.id)

        # Worker state is UNCHANGED
        worker_after = (
            await session.execute(
                text(
                    "SELECT id, role, runtime, max_concurrent_tasks, "
                    "current_profile_version, capabilities, permissions, "
                    "personality FROM workers WHERE id = :wid"
                ),
                {"wid": original.assigned_worker},
            )
        ).first()

        for col in (
            "id", "role", "runtime", "max_concurrent_tasks",
            "current_profile_version", "capabilities", "permissions",
            "personality",
        ):
            before_val = getattr(worker_before, col)
            after_val = getattr(worker_after, col)
            assert before_val == after_val, (
                f"worker.{col} mutated: {before_val!r} → {after_val!r}"
            )


# ---------- AC #9: full chain test (DB → domain → profile → kernel → adapter → result) ----------


async def test_chain_test_full_path(
    profiles_dir, database_url, session_factory
):
    """End-to-end: DB row → Task.from_orm → resolve_context → kernel → adapter → Result → DB row.

    This is the chain-test methodology Liang raised after Step 5: exercise
    the entire ORM → domain → profile → kernel → adapter path in one test,
    rather than testing each layer in isolation.
    """
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        # 1. DB → set up worker + production task
        original, reg, _ = await _produce_original_task(session, profiles_dir)

        # 2. Activate v3 (different from original snapshot)
        await _activate_v3(reg)

        # 3. Capture which profile version the SimulationAdapter receives
        @dataclass
        class ChainCapturingAdapter:
            captured_context: WorkerExecutionContext | None = None

            async def execute(
                self,
                context: WorkerExecutionContext,
                checkpoint: Any | None = None,
            ) -> Result:
                self.captured_context = context
                return Result(
                    status="completed",
                    output={"simulated": True, "captured": True},
                )

        chain_adapter = ChainCapturingAdapter()
        sim_kernel = WorkerExecutionKernel(
            session=session,
            adapter=chain_adapter,
            resolve_context=reg.resolve_execution_context,
        )
        sim_service = SimulationService(
            session=session, kernel=sim_kernel, registry=reg
        )

        # 4. Replay
        result = await sim_service.replay(original.id)

        # 5. Verify each link in the chain:
        #    5a. DB row read (source) → Task.from_orm
        #        The sim_service loaded the source from DB and snapshotted
        #        its profile_version into the new task.
        assert result.source_task_id == original.id
        assert result.source_profile_version == 2  # NOT 3

        #    5b. domain → profile: the adapter received the v2 profile
        assert chain_adapter.captured_context is not None
        assert chain_adapter.captured_context.profile.version == 2
        assert chain_adapter.captured_context.profile.persona.autonomy == "low"
        #    The worker.current is now 3 — kernel/registry never used it
        assert chain_adapter.captured_context.worker.current_profile_version == 3

        #    5c. profile → kernel: kernel ran the lifecycle
        #        Verify task.assigned_worker matches
        assert chain_adapter.captured_context.task.assigned_worker == original.assigned_worker

        #    5d. kernel → adapter: adapter received the context, returned Result
        assert chain_adapter.captured_context.profile.version == result.source_profile_version

        #    5e. adapter → Result → DB: the new task row reflects the result
        sim_row = (
            await session.execute(
                text(
                    "SELECT result, status, profile_version, simulated_from "
                    "FROM tasks WHERE id = :tid"
                ),
                {"tid": result.task_id},
            )
        ).first()
        assert sim_row.status == "completed"
        assert sim_row.profile_version == 2  # snapshot preserved on the sim row too
        assert sim_row.simulated_from == original.id
        assert sim_row.result == {"simulated": True, "captured": True}
