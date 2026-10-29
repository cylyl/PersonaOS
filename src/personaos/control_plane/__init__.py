"""Control-plane distribution and cluster coordination package.

Per ADR 0033 — Multi-Node Control-Plane Distribution.
"""

from personaos.control_plane.advisory_lock import (
    LOCK_ID_CP_REAPER,
    LOCK_ID_NODE_SUPERVISOR,
    LOCK_ID_ORPHAN_REAPER,
    LOCK_ID_TELEMETRY_AGGREGATOR,
    advisory_xact_lock,
    try_advisory_xact_lock,
)
from personaos.control_plane.registry import ControlPlaneNodeInfo, ControlPlaneRegistry

__all__ = [
    "ControlPlaneRegistry",
    "ControlPlaneNodeInfo",
    "advisory_xact_lock",
    "try_advisory_xact_lock",
    "LOCK_ID_ORPHAN_REAPER",
    "LOCK_ID_NODE_SUPERVISOR",
    "LOCK_ID_TELEMETRY_AGGREGATOR",
    "LOCK_ID_CP_REAPER",
]
