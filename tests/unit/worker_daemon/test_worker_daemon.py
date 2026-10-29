"""Unit tests for Remote Worker Daemon & Heartbeat Distribution (v0.2-B).

Per ADR 0034 and docs/specs/remote-worker-daemon-heartbeat-distribution.md.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from personaos.worker_daemon.capabilities import SystemCapabilityScanner
from personaos.worker_daemon.config import DaemonConfig
from personaos.worker_daemon.idempotency import StepIdempotency
from personaos.worker_daemon.sandbox import ProcessSandboxSupervisor


def test_system_capability_scanner() -> None:
    """Verifies host capabilities discovery."""
    caps = SystemCapabilityScanner.scan()
    assert caps["cpu_cores"] >= 1
    assert caps["memory_gb"] > 0
    assert "cpu_arch" in caps
    assert "supported_runtimes" in caps
    assert "native_process" in caps["supported_runtimes"]


def test_daemon_config_defaults() -> None:
    """Verifies default intervals and timing contracts match ADR 0034."""
    cfg = DaemonConfig(worker_id="worker-devops-01")
    assert cfg.heartbeat_interval == 5.0
    assert cfg.heartbeat_jitter_max == 1.0
    assert cfg.node_timeout == 15.0
    assert cfg.task_lease_ttl == 30.0
    assert cfg.drain_timeout == 60.0
    assert cfg.node_id.startswith("node-")


def test_step_idempotency_mapping() -> None:
    """Verifies mapping of side effect classifications to StepIdempotency."""
    assert StepIdempotency.from_classification("IDEMPOTENT") == StepIdempotency.STRICTLY_IDEMPOTENT
    assert StepIdempotency.from_classification("HAS_SIDE_EFFECTS") == StepIdempotency.NON_IDEMPOTENT
    assert StepIdempotency.from_classification("UNKNOWN") == StepIdempotency.NON_IDEMPOTENT
    assert StepIdempotency.from_classification("") == StepIdempotency.NON_IDEMPOTENT


@pytest.mark.asyncio
async def test_process_sandbox_spawn_and_clean_termination() -> None:
    """Verifies ProcessSandboxSupervisor launches in process group and cleans up on terminate."""
    supervisor = ProcessSandboxSupervisor()
    task_id = "task-sandbox-test-1"

    # Spawn a sleeping python subprocess
    cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
    proc = await supervisor.spawn_sandbox(task_id, cmd)

    assert proc.pid > 0
    assert supervisor.is_active(task_id) is True

    # Terminate sandbox
    terminated = await supervisor.terminate_sandbox(task_id, grace_period_sec=0.5)
    assert terminated is True
    assert supervisor.is_active(task_id) is False


@pytest.mark.asyncio
async def test_fail_closed_abort_kills_process() -> None:
    """Verifies fail_closed_abort immediately halts sandbox processes on fencing rejection."""
    supervisor = ProcessSandboxSupervisor()
    task_id = "task-fenced-abort-test"

    cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
    proc = await supervisor.spawn_sandbox(task_id, cmd)
    assert supervisor.is_active(task_id) is True

    await supervisor.fail_closed_abort(task_id, reason="stale_generation_fenced")
    assert supervisor.is_active(task_id) is False
    assert proc.returncode is not None
