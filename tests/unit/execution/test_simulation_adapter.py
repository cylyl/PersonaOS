"""Unit tests for SimulationAdapter.

Per ADR 0011 and docs/specs/simulation-and-replay.md.
"""

from __future__ import annotations

try:
    import pytest
except ImportError:
    import contextlib

    class _MockMark:
        @staticmethod
        def asyncio(fn):
            return fn

    class _MockPytest:
        mark = _MockMark()

        @staticmethod
        def raises(exc_type, match=None):
            @contextlib.contextmanager
            def _cm():
                try:
                    yield
                except exc_type as e:
                    if match and match not in str(e):
                        raise AssertionError(
                            f"Expected {match!r} in {str(e)!r}"
                        ) from e
                else:
                    raise AssertionError(
                        f"Expected {exc_type.__name__} was not raised"
                    )

            return _cm()

    pytest = _MockPytest()  # type: ignore[assignment]

from personaos.domain.task import Task
from personaos.domain.worker import Persona, Worker, WorkerProfile
from personaos.execution.adapters.base import Adapter
from personaos.execution.adapters.simulation import SimulationAdapter
from personaos.execution.context import WorkerExecutionContext
from personaos.execution.result import Result


def _make_dummy_context(profile_version: int = 1) -> WorkerExecutionContext:
    worker = Worker(
        id="worker-01",
        name="Dev Worker",
        role="developer",
        status="active",
        runtime="openclaw",
        max_concurrent_tasks=1,
        current_profile_version=profile_version,
    )
    profile = WorkerProfile(
        version=profile_version,
        role="developer",
        runtime="openclaw",
        persona=Persona(autonomy="medium"),
    )
    task = Task(
        id="task-01",
        title="Test Task",
        objective="Verify simulation",
        input={"cmd": "run"},
        profile_version=profile_version,
        status="queued",
        priority="medium",
        assigned_worker="worker-01",
    )
    return WorkerExecutionContext(worker=worker, profile=profile, task=task)


@pytest.mark.asyncio
async def test_simulation_adapter_conforms_to_protocol():
    adapter = SimulationAdapter()
    assert isinstance(adapter, Adapter)


@pytest.mark.asyncio
async def test_simulation_adapter_canned_result():
    expected = Result(status="completed", output={"msg": "canned"})
    adapter = SimulationAdapter(result=expected)
    ctx = _make_dummy_context()

    res = await adapter.execute(ctx, checkpoint={"step": 1})
    assert res.status == "completed"
    assert res.output == {"msg": "canned"}
    assert adapter.call_count == 1
    assert adapter.captured_contexts == [ctx]
    assert adapter.captured_checkpoints == [{"step": 1}]


@pytest.mark.asyncio
async def test_simulation_adapter_handler_sync_and_async():
    # Sync handler
    sync_adapter = SimulationAdapter(
        handler=lambda ctx, cp: Result(
            status="completed", output={"role": ctx.profile.role}
        )
    )
    ctx = _make_dummy_context()
    res = await sync_adapter.execute(ctx)
    assert res.output == {"role": "developer"}

    # Async handler
    async def async_handler(c, cp):
        return Result(status="failed", error="simulated failure")

    async_adapter = SimulationAdapter(handler=async_handler)
    res_async = await async_adapter.execute(ctx)
    assert res_async.status == "failed"
    assert res_async.error == "simulated failure"


@pytest.mark.asyncio
async def test_simulation_adapter_sequence():
    seq = [
        Result(status="in_progress", new_checkpoint={"step": 1}),
        Result(status="completed", output={"done": True}),
    ]
    adapter = SimulationAdapter(sequence=seq)
    ctx = _make_dummy_context()

    r1 = await adapter.execute(ctx, checkpoint=None)
    assert r1.new_checkpoint == {"step": 1}

    r2 = await adapter.execute(ctx, checkpoint={"step": 1})
    assert r2.status == "completed"
    assert r2.output == {"done": True}

    with pytest.raises(RuntimeError, match="exhausted"):
        await adapter.execute(ctx)


@pytest.mark.asyncio
async def test_simulation_adapter_error_trigger():
    adapter = SimulationAdapter(error_trigger=ValueError("boom"))
    ctx = _make_dummy_context()

    with pytest.raises(ValueError, match="boom"):
        await adapter.execute(ctx)


@pytest.mark.asyncio
async def test_simulation_adapter_default_deterministic_output():
    adapter = SimulationAdapter()
    ctx = _make_dummy_context(profile_version=2)

    res = await adapter.execute(ctx)
    assert res.status == "completed"
    assert res.output == {
        "simulated": True,
        "worker_id": "worker-01",
        "profile_version": 2,
        "role": "developer",
        "task_id": "task-01",
    }


def test_simulation_adapter_reset():
    adapter = SimulationAdapter()
    ctx = _make_dummy_context()
    adapter.captured_contexts.append(ctx)
    adapter.call_count = 5
    adapter.reset()

    assert adapter.call_count == 0
    assert len(adapter.captured_contexts) == 0
