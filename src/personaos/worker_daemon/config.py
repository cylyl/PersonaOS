"""Configuration for remote worker daemons.

Per ADR 0034 — Remote Worker Daemon and Heartbeat Distribution.
"""

from __future__ import annotations

import socket
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DaemonConfig:
    """Runtime configuration for a personaos-worker-daemon instance."""

    worker_id: str
    node_id: str = field(
        default_factory=lambda: f"node-{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
    )
    control_plane_url: str = "http://127.0.0.1:8000"
    heartbeat_interval: float = 5.0
    heartbeat_jitter_max: float = 1.0
    node_timeout: float = 15.0
    task_lease_ttl: float = 30.0
    drain_timeout: float = 60.0
    max_concurrent_tasks: int = 1
    sandbox_runtime: str = "process"
    extra_capabilities: dict[str, Any] = field(default_factory=dict)
