"""Unit tests for FastAPI control-plane supervision daemon lifecycle."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personaos.api.app import create_app
from personaos.config import settings


class MockSessionContext:
    """Mock async context manager mimicking AsyncSession from session_factory()."""

    async def __aenter__(self) -> AsyncMock:
        s = AsyncMock()
        mock_result = MagicMock()
        mock_result.first.return_value = ["0006_security_events"]
        s.execute.return_value = mock_result
        return s

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass


def make_mock_session_factory() -> MagicMock:
    return MagicMock(side_effect=MockSessionContext)


@pytest.mark.asyncio
async def test_supervision_daemon_starts_and_stops_cleanly() -> None:
    """LIFECYCLE-01: Control plane lifespan starts NodeSupervisor and terminates cleanly."""
    mock_session_factory = make_mock_session_factory()
    mock_engine = AsyncMock()

    app = create_app(engine=mock_engine, session_factory=mock_session_factory)

    async with app.router.lifespan_context(app):
        # Check supervisor task was created and is running
        assert hasattr(app.state, "supervisor_task")
        supervisor_task = app.state.supervisor_task
        assert supervisor_task is not None
        assert not supervisor_task.done()
        assert hasattr(app.state, "supervisor")
        assert hasattr(app.state, "supervisor_stop_event")

    # Exiting lifespan triggers shutdown
    assert supervisor_task.done()


@pytest.mark.asyncio
async def test_supervision_daemon_disabled_flag() -> None:
    """LIFECYCLE-02: Setting enable_node_supervision=False disables the background daemon."""
    mock_session_factory = make_mock_session_factory()
    mock_engine = AsyncMock()

    with patch.object(settings, "enable_node_supervision", False):
        app = create_app(engine=mock_engine, session_factory=mock_session_factory)

        async with app.router.lifespan_context(app):
            assert not hasattr(app.state, "supervisor_task")


@pytest.mark.asyncio
async def test_supervision_daemon_loop_resilience_to_errors() -> None:
    """LIFECYCLE-03: Exceptions in a cycle iteration do not terminate the background loop."""
    mock_session_factory = make_mock_session_factory()
    mock_engine = AsyncMock()

    call_count = 0
    cycle_executed = asyncio.Event()

    async def flaky_cycle(**kwargs: object) -> None:
        nonlocal call_count
        call_count += 1
        cycle_executed.set()
        if call_count == 1:
            raise RuntimeError("Transient database glitch")

    with (
        patch.object(settings, "node_supervision_interval_seconds", 0.05),
        patch("personaos.api.app.NodeSupervisor") as mock_sup_cls,
    ):
        mock_sup_instance = mock_sup_cls.return_value
        from personaos.orchestrator.supervisor import NodeSupervisor

        real_sup = NodeSupervisor(session_factory=mock_session_factory)
        real_sup.supervise_cycle = flaky_cycle  # type: ignore[method-assign]
        mock_sup_instance.run_loop = real_sup.run_loop

        app = create_app(engine=mock_engine, session_factory=mock_session_factory)

        async with app.router.lifespan_context(app):
            # Wait for cycle to execute
            await asyncio.wait_for(cycle_executed.wait(), timeout=2.0)
            # Give time for second cycle to run despite first cycle error
            await asyncio.sleep(0.15)
            assert call_count >= 2
            assert not app.state.supervisor_task.done()
