"""Worker domain — thin identity record + versioned WorkerProfile.

Per ADR 0003 + ADR 0008 Addendum:

  - Worker is identity (survives model + runtime changes)
  - WorkerProfile is a versioned, immutable assignment of Persona + config
  - Persona ≠ WorkerProfile (personality ≠ operational profile)

Per docs/specs/worker-profile.md:
  - Profile files are immutable YAML on disk; the DB stores only the
    active pointer (workers.current_profile_version)
  - tasks.profile_version captures the snapshot at enqueue time, never updated
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Persona:
    """Behavioral identity of a worker.

    Independent of operational concerns. Stable across most operational
    changes — a worker can switch runtime or adjust max_concurrent_tasks
    without that being a "persona change."

    Per ADR 0008 Addendum: Personality ≠ operational profile. This
    dataclass only carries the behavioral subset.
    """

    style: str | None = None
    communication: str | None = None
    autonomy: str | None = None  # one of: "low" | "medium" | "high"
    behavior: dict[str, Any] = field(default_factory=dict)
    goals: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.autonomy is not None and self.autonomy not in ("low", "medium", "high"):
            raise ValueError(f"persona.autonomy must be low/medium/high, got {self.autonomy!r}")


@dataclass(frozen=True)
class WorkerProfile:
    """Versioned, immutable assignment of Persona + config to a Worker.

    Per docs/specs/worker-profile.md Section 2 (YAML Schema).
    Once written to disk (profiles/{worker_id}/v{N}.yaml), immutable. To change
    v2, create v3. v2 stays as-is forever.
    """

    version: int
    role: str
    runtime: str
    max_concurrent_tasks: int = 1
    description: str | None = None
    persona: Persona = field(default_factory=Persona)
    model: str | None = None  # LLM model reference (placeholder in v0.1)
    instructions: str | None = None
    skills: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError(f"version must be >= 1, got {self.version}")
        if self.max_concurrent_tasks < 1:
            raise ValueError(f"max_concurrent_tasks must be >= 1, got {self.max_concurrent_tasks}")


@dataclass(frozen=True)
class Worker:
    """In-memory representation of a worker record.

    Returned by ProfileRegistry.get_worker(). The DB row (Worker ORM model
    in db/models.py) is the canonical record.
    """

    id: str
    name: str
    role: str
    status: str
    runtime: str
    max_concurrent_tasks: int
    current_profile_version: int
    description: str | None = None
    capabilities: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    personality: dict[str, Any] = field(default_factory=dict)
    memory_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_seen_at: datetime | None = None
