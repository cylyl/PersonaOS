"""Step 6 — Simulation Adapter and Task Replay acceptance tests.

Per docs/specs/simulation-and-replay.md (ADR 0011).

Acceptance criteria:
  1. SimulationAdapter conforms to Adapter Protocol
  2. Kernel executes successfully with SimulationAdapter
  3. Kernel handles SimulationAdapter exceptions as task failure
  4. Checkpoint sequence passes through SimulationAdapter
  5. Replay loads exact historical snapshot profile
  6. Counterfactual replay uses overridden profile without mutating DB
  7. Historical task immutability during replay
  8. Fork-and-replay creates independent runnable task
  9. Enqueue emits task.created event
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from personaos.db.models import Task as TaskModel
from personaos.domain.task import Task
from personaos.domain.worker import Persona, WorkerProfile
from personaos.execution.adapters.base import Adapter, Result
from personaos.execution.adapters.simulation import SimulationAdapter
from personaos.execution.kernel import WorkerExecutionKernel
from personaos.registry.profile import ProfileRegistry
from personaos.simulation.service import ReplayResult, ReplayService
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


@pytest.fixture
def profiles_dir(tmp_path, monkeypatch):
    d = tmp_path / "profiles"
    monkeypatch.setenv("PERSONAOS_PROFILES_DIR", str(d))
    return d


async def _setup_worker_v1_v2(
    session, profiles_dir, *, worker_id: str = "w1"
) -> ProfileRegistry:
    reg = ProfileRegistry(session=session, profiles_dir=profiles_dir)
    await reg.register_worker(
        worker_id,
        WorkerProfile(
            version=1,
            role="devops",
            runtime="openclaw",
            persona=Persona(autonomy="low", communication="concise"),
        ),
    )
    await reg.create_profile_version(
        worker_id,
        WorkerProfile(
            version=2,
            role="devops",
            runtime="openclaw",
            persona=Persona(autonomy="high", communication="verbose"),
        ),
    )
    await reg.activate_profile_version(worker_id, 1)
    return reg


async def test_simulation_adapter_protocol_conformance():
    """AC #1: SimulationAdapter implements Adapter Protocol."""
    adapter = SimulationAdapter()
    assert isinstance(adapter, Adapter)


async def test_simulation_adapter_with_kernel_success(
    profiles_dir, database_url, session_factory
):
    """AC #2: Kernel executes a queued task using SimulationAdapter."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        reg = await _setup_worker_v1_v2(session, profiles_dir)
        wl = WorkloadService(session)
        task = await wl.enqueue(
            worker_id="w1",
            title="Deploy Staging",
            objective="Deploy service",
            input={"cluster": "staging"},
        )

        sim_adapter = SimulationAdapter(
            result=Result(
                status="completed",
                output={"deployed": True, "env": "staging"},
            )
        )
        kernel = WorkerExecutionKernel(
            session=session,
            adapter=sim_adapter,
            resolve_context=reg.resolve_execution_context,
        )

        result = await kernel.execute(task)
        assert result.status == "completed"
        assert result.output == {"deployed": True, "env": "staging"}
        assert sim_adapter.call_count == 1
        assert sim_adapter.captured_contexts[0].profile.version == 1

        # Verify DB status
        status = (
            await session.execute(
                text("SELECT status FROM tasks WHERE id = :id"),
                {"id": task.id},
            )
        ).scalar()
        assert status == "completed"


async def test_simulation_adapter_with_kernel_exception(
    profiles_dir, database_url, session_factory
):
    """AC #3: Kernel handles exceptions raised by SimulationAdapter gracefully."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        reg = await _setup_worker_v1_v2(session, profiles_dir)
        wl = WorkloadService(session)
        task = await wl.enqueue(
            worker_id="w1",
            title="Failing Task",
            objective="Crash",
            input={},
        )

        sim_adapter = SimulationAdapter(error_trigger=RuntimeError("simulated crash"))
        kernel = WorkerExecutionKernel(
            session=session,
            adapter=sim_adapter,
            resolve_context=reg.resolve_execution_context,
        )

        result = await kernel.execute(task)
        assert result.status == "failed"
        assert "RuntimeError: simulated crash" in (result.error or "")

        status = (
            await session.execute(
                text("SELECT status FROM tasks WHERE id = :id"),
                {"id": task.id},
            )
        ).scalar()
        assert status == "failed"


async def test_simulation_adapter_sequence_with_checkpoints(
    profiles_dir, database_url, session_factory
):
    """AC #4: Sequence of results with intermediate checkpoints works properly."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        reg = await _setup_worker_v1_v2(session, profiles_dir)
        wl = WorkloadService(session)
        task = await wl.enqueue(
            worker_id="w1",
            title="Step-by-step Task",
            objective="Multi-step checkpointing",
            input={},
        )

        sim_adapter = SimulationAdapter(
            sequence=[
                Result(
                    status="completed",
                    output={"step": 1, "done": True},
                    new_checkpoint={"progress": 100},
                )
            ]
        )
        kernel = WorkerExecutionKernel(
            session=session,
            adapter=sim_adapter,
            resolve_context=reg.resolve_execution_context,
        )

        result = await kernel.execute(task)
        assert result.status == "completed"
        assert result.new_checkpoint == {"progress": 100}

        cp_ref = (
            await session.execute(
                text("SELECT checkpoint_ref FROM tasks WHERE id = :id"),
                {"id": task.id},
            )
        ).scalar()
        assert cp_ref is not None


async def test_replay_uses_historical_task_snapshot(
    profiles_dir, database_url, session_factory
):
    """AC #5: Replay loads exact historical snapshot profile, even after activation."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        reg = await _setup_worker_v1_v2(session, profiles_dir)
        wl = WorkloadService(session)

        # Enqueue task under v1
        task = await wl.enqueue(
            worker_id="w1",
            title="Original Task",
            objective="Run under v1",
            input={"key": "val"},
        )
        assert task.profile_version == 1

        # Execute task to completion
        sim_adapter = SimulationAdapter(
            result=Result(status="completed", output={"res": 1})
        )
        kernel = WorkerExecutionKernel(
            session=session,
            adapter=sim_adapter,
            resolve_context=reg.resolve_execution_context,
        )
        await kernel.execute(task)

        # Now activate v2 on the worker
        await reg.activate_profile_version("w1", 2)

        # Replay the historical task
        replay_service = ReplayService(session, reg)
        replay_adapter = SimulationAdapter(
            result=Result(status="completed", output={"res": 1})
        )
        replay_res = await replay_service.replay(task.id, adapter=replay_adapter)

        assert replay_res.matched is True
        assert replay_res.original_profile_version == 1
        assert replay_res.executed_profile_version == 1
        assert replay_adapter.captured_contexts[0].profile.version == 1
        assert (
            replay_adapter.captured_contexts[0].profile.persona.autonomy
            == "low"
        )


async def test_counterfactual_replay_profile_override(
    profiles_dir, database_url, session_factory
):
    """AC #6: Replay under profile_version_override runs counterfactual simulation."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        reg = await _setup_worker_v1_v2(session, profiles_dir)
        wl = WorkloadService(session)

        # Task enqueued under v1, completed with output {"v": 1}
        task = await wl.enqueue(
            worker_id="w1",
            title="Historical Task",
            objective="Test v1 vs v2",
            input={},
        )
        kernel = WorkerExecutionKernel(
            session=session,
            adapter=SimulationAdapter(
                result=Result(status="completed", output={"v": 1})
            ),
            resolve_context=reg.resolve_execution_context,
        )
        await kernel.execute(task)

        # Replay with override to v2
        replay_service = ReplayService(session, reg)
        eval_adapter = SimulationAdapter(
            result=Result(status="completed", output={"v": 2})
        )
        report = await replay_service.replay(
            task.id, adapter=eval_adapter, profile_version_override=2
        )

        assert report.original_profile_version == 1
        assert report.executed_profile_version == 2
        assert report.matched is False
        assert report.diff["output"]["original"] == {"v": 1}
        assert report.diff["output"]["replayed"] == {"v": 2}
        assert eval_adapter.captured_contexts[0].profile.version == 2
        assert (
            eval_adapter.captured_contexts[0].profile.persona.autonomy
            == "high"
        )


async def test_historical_task_is_not_mutated_by_replay(
    profiles_dir, database_url, session_factory
):
    """AC #7: Replay execution does not touch historical DB record."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        reg = await _setup_worker_v1_v2(session, profiles_dir)
        wl = WorkloadService(session)

        task = await wl.enqueue(
            worker_id="w1",
            title="Task To Preserve",
            objective="Do not mutate",
            input={"x": 10},
        )
        kernel = WorkerExecutionKernel(
            session=session,
            adapter=SimulationAdapter(
                result=Result(status="completed", output={"val": 10})
            ),
            resolve_context=reg.resolve_execution_context,
        )
        await kernel.execute(task)

        row_before = (
            await session.execute(
                text(
                    "SELECT status, result, completed_at FROM tasks WHERE id = :id"
                ),
                {"id": task.id},
            )
        ).first()

        replay_service = ReplayService(session, reg)
        await replay_service.replay(
            task.id,
            adapter=SimulationAdapter(
                result=Result(status="failed", error="replayed failure")
            ),
        )

        row_after = (
            await session.execute(
                text(
                    "SELECT status, result, completed_at FROM tasks WHERE id = :id"
                ),
                {"id": task.id},
            )
        ).first()

        assert row_after.status == row_before.status == "completed"
        assert row_after.result == row_before.result == {"val": 10}
        assert row_after.completed_at == row_before.completed_at


async def test_fork_and_replay_enqueues_new_task(
    profiles_dir, database_url, session_factory
):
    """AC #8: Fork-and-replay enqueues a new independent queued task."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        reg = await _setup_worker_v1_v2(session, profiles_dir)
        wl = WorkloadService(session)

        original_task = await wl.enqueue(
            worker_id="w1",
            title="Base Task",
            objective="Deploy FMS",
            input={"flag": True},
        )

        replay_service = ReplayService(session, reg)
        forked = await replay_service.fork_and_replay(
            original_task.id, profile_version_override=2
        )

        assert forked.id != original_task.id
        assert forked.title == "[Replay] Base Task"
        assert forked.objective == "Deploy FMS"
        assert forked.type == "replay"
        assert forked.status == "queued"
        assert forked.profile_version == 2


async def test_enqueue_emits_task_created_event(
    profiles_dir, database_url, session_factory
):
    """AC #9: WorkloadService.enqueue emits task.created event."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with session_factory() as session:
        await _setup_worker_v1_v2(session, profiles_dir)
        wl = WorkloadService(session)

        task = await wl.enqueue(
            worker_id="w1",
            title="Event check",
            objective="Verify task.created event",
            input={},
        )

        events = (
            await session.execute(
                text(
                    "SELECT type, actor_id FROM task_events WHERE task_id = :id"
                ),
                {"id": task.id},
            )
        ).all()

        assert len(events) == 1
        assert events[0].type == "task.created"
        assert events[0].actor_id == "w1"
