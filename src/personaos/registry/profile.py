"""ProfileRegistry — owns the WorkerProfile lifecycle.

Per ADR 0008 Addendum + docs/specs/worker-profile.md:

The ONLY writer to the profiles/ directory.
Activation is atomic (single DB transaction).
Profile files are immutable after creation.
Registry validates + references the file, never mutates it.

Activation guard (per Liang, 2026-10-07):
  create v2
    ↓
  validate/reference-check (file exists, parses)
    ↓
  atomic transaction
    ├── set workers.current_profile_version = 2
    └── commit
    ↓
  do NOT mutate v1

The profile version records the configuration ASSIGNED to the task,
not necessarily the configuration that ultimately EXECUTED it.
This distinction will matter when retries/failover are added in v0.2+.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from personaos.db.models import Worker as WorkerModel
from personaos.domain.task import Task
from personaos.domain.worker import Persona, WorkerProfile, Worker
from personaos.execution.context import WorkerExecutionContext


# Default profiles directory; can be overridden via env var or constructor arg.
# Layout: {profiles_dir}/{worker_id}/v{N}.yaml
DEFAULT_PROFILES_DIR = Path(
    os.environ.get("PERSONAOS_PROFILES_DIR", "profiles")
)


@dataclass(frozen=True)
class ProfileVersion:
    """Locator for a specific profile version on disk."""

    worker_id: str
    version: int
    path: Path


class ProfileError(Exception):
    """Base class for profile registry errors."""


class ProfileVersionExistsError(ProfileError):
    """Raised when create_profile_version targets an existing version."""


class ProfileVersionNotFoundError(ProfileError):
    """Raised when a referenced profile file does not exist."""


class WorkerNotFoundError(ProfileError):
    """Raised when a referenced worker_id does not exist."""


class ProfileRegistry:
    """Owns the WorkerProfile lifecycle.

    The ONLY writer to the profiles/ directory.
    Reads/writes workers.current_profile_version atomically.
    """

    def __init__(
        self,
        session: AsyncSession,
        profiles_dir: Optional[Path] = None,
    ) -> None:
        self._session = session
        self._profiles_dir = profiles_dir or DEFAULT_PROFILES_DIR

    # --- File-system operations (sync, never block on the DB) ---

    def _profile_path(self, worker_id: str, version: int) -> Path:
        """profiles/{worker_id}/v{version}.yaml"""
        return self._profiles_dir / worker_id / f"v{version}.yaml"

    def _write_profile_file(self, worker_id: str, profile: WorkerProfile) -> Path:
        """Write the profile YAML to disk. IMMUTABLE — rejects overwrite.

        Immutability invariant (Section 8, #2): a profile file is written
        exactly once. If you want to "change" v2, create v3. v2 stays
        as-is forever.
        """
        path = self._profile_path(worker_id, profile.version)
        if path.exists():
            raise ProfileVersionExistsError(
                f"profile {path} already exists; create v{profile.version + 1} instead"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_dump_profile_yaml(profile), encoding="utf-8")
        return path

    def get_profile_version(self, worker_id: str, version: int) -> WorkerProfile:
        """Load a specific historical version (immutable read)."""
        path = self._profile_path(worker_id, version)
        if not path.exists():
            raise ProfileVersionNotFoundError(
                f"profile {path} does not exist"
            )
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return _load_profile_yaml(data)

    def list_profile_versions(self, worker_id: str) -> list[int]:
        """Return sorted list of version numbers on disk."""
        worker_dir = self._profiles_dir / worker_id
        if not worker_dir.exists():
            return []
        versions: list[int] = []
        for p in worker_dir.glob("v*.yaml"):
            try:
                versions.append(int(p.stem.lstrip("v")))
            except ValueError:
                continue
        return sorted(versions)

    # --- DB operations (async) ---

    async def register_worker(
        self,
        worker_id: str,
        initial_profile: WorkerProfile,
    ) -> None:
        """Write v1.yaml + insert workers row. Idempotent on worker_id.

        Raises ValueError if initial_profile.version != 1.
        Raises ProfileError if the worker already exists.
        """
        if initial_profile.version != 1:
            raise ValueError(
                f"register_worker requires version=1, got {initial_profile.version}"
            )

        existing = await self._session.execute(
            select(WorkerModel).where(WorkerModel.id == worker_id)
        )
        if existing.scalar_one_or_none() is not None:
            raise ProfileError(f"worker {worker_id} already exists")

        # 1. Write v1.yaml
        self._write_profile_file(worker_id, initial_profile)
        # 2. Insert workers row
        worker = WorkerModel(
            id=worker_id,
            name=worker_id,
            role=initial_profile.role,
            description=initial_profile.description,
            status="idle",
            runtime=initial_profile.runtime,
            max_concurrent_tasks=initial_profile.max_concurrent_tasks,
            capabilities=list(initial_profile.skills),
            permissions=list(initial_profile.permissions),
            personality=_persona_to_dict(initial_profile.persona),
            current_profile_version=1,
        )
        self._session.add(worker)
        await self._session.commit()

    async def get_worker(self, worker_id: str) -> Worker:
        """Read the workers row + return as a Worker dataclass."""
        result = await self._session.execute(
            select(WorkerModel).where(WorkerModel.id == worker_id)
        )
        worker = result.scalar_one_or_none()
        if worker is None:
            raise WorkerNotFoundError(f"worker {worker_id} not found")
        return _worker_from_orm(worker)

    async def get_current_profile(self, worker_id: str) -> WorkerProfile:
        """Load profiles/{id}/v{current_profile_version}.yaml."""
        worker = await self.get_worker(worker_id)
        return self.get_profile_version(worker_id, worker.current_profile_version)

    async def create_profile_version(
        self,
        worker_id: str,
        profile: WorkerProfile,
    ) -> ProfileVersion:
        """Validate + write v(N+1).yaml. Does NOT activate.

        Immutability guard: rejects overwrite of an existing version.
        Monotonic versioning: profile.version must be > max existing.
        """
        await self.get_worker(worker_id)
        existing = self.list_profile_versions(worker_id)
        if existing and profile.version <= max(existing):
            raise ValueError(
                f"profile version {profile.version} must be greater than "
                f"the latest existing version ({max(existing)})"
            )
        if profile.version < 1:
            raise ValueError(f"version must be >= 1, got {profile.version}")
        path = self._write_profile_file(worker_id, profile)
        return ProfileVersion(
            worker_id=worker_id, version=profile.version, path=path
        )

    async def activate_profile_version(
        self,
        worker_id: str,
        version: int,
    ) -> None:
        """Atomic activation. NEVER touches the profile file.

        Activation guard (per Liang, 2026-10-07):
            1. validate/reference-check (file exists, parses) — READ ONLY
            2. atomic transaction (workers.current_profile_version + operational fields)
            3. commit
            4. do NOT mutate v1 (or any existing file)

        Idempotent: activating the currently-active version is a no-op
        (DB update writes the same values, file is untouched either way).
        """
        # 1. Validate the file exists and parses (READ ONLY)
        profile = self.get_profile_version(worker_id, version)

        # 2. Atomic DB update — single transaction
        await self._session.execute(
            update(WorkerModel)
            .where(WorkerModel.id == worker_id)
            .values(
                role=profile.role,
                description=profile.description,
                runtime=profile.runtime,
                max_concurrent_tasks=profile.max_concurrent_tasks,
                capabilities=list(profile.skills),
                permissions=list(profile.permissions),
                personality=_persona_to_dict(profile.persona),
                current_profile_version=version,
            )
        )
        await self._session.commit()
        # NOTE: profile file at profiles/{id}/v{version}.yaml is NEVER touched

    async def resolve_execution_context(self, task: Task) -> WorkerExecutionContext:
        """Load worker + snapshot profile, return a frozen context.

        NEVER reads worker.current_profile_version. ALWAYS reads task.profile_version.

        This is the ONLY way the runtime gets a profile — there is no
        "load current profile" path in v0.1. If you find yourself wanting
        to add one, step back and re-read ADR 0009 first.

        Raises ProfileError if task has no assigned_worker.
        Raises WorkerNotFoundError if the worker is gone.
        Raises ProfileVersionNotFoundError if the snapshot version is missing on disk.
        """
        if not task.assigned_worker:
            raise ProfileError(
                f"task {task.id} has no assigned_worker; cannot resolve context"
            )
        worker = await self.get_worker(task.assigned_worker)
        profile = self.get_profile_version(worker.id, task.profile_version)
        return WorkerExecutionContext(worker=worker, profile=profile, task=task)


# --- Module-level helpers (manual YAML serialization for v0.1) ---


def _persona_to_dict(persona: Persona) -> dict:
    """Serialize Persona → JSONB-friendly dict."""
    return {
        "style": persona.style,
        "communication": persona.communication,
        "autonomy": persona.autonomy,
        "behavior": dict(persona.behavior),
        "goals": list(persona.goals),
        "constraints": list(persona.constraints),
    }


def _dump_profile_yaml(profile: WorkerProfile) -> str:
    """Serialize WorkerProfile to YAML. Manual (no pydantic for v0.1)."""
    data = {
        "version": profile.version,
        "role": profile.role,
        "description": profile.description,
        "persona": _persona_to_dict(profile.persona),
        "model": profile.model,
        "instructions": profile.instructions,
        "skills": list(profile.skills),
        "permissions": list(profile.permissions),
        "runtime": profile.runtime,
        "max_concurrent_tasks": profile.max_concurrent_tasks,
        "metadata": dict(profile.metadata),
    }
    return yaml.safe_dump(
        _strip_empty(data), sort_keys=False, allow_unicode=True
    )


def _load_profile_yaml(data: dict) -> WorkerProfile:
    """Parse YAML dict → WorkerProfile. Manual (no pydantic for v0.1)."""
    persona_data = data.get("persona") or {}
    persona = Persona(
        style=persona_data.get("style"),
        communication=persona_data.get("communication"),
        autonomy=persona_data.get("autonomy"),
        behavior=persona_data.get("behavior") or {},
        goals=persona_data.get("goals") or [],
        constraints=persona_data.get("constraints") or [],
    )
    return WorkerProfile(
        version=data["version"],
        role=data["role"],
        description=data.get("description"),
        persona=persona,
        model=data.get("model"),
        instructions=data.get("instructions"),
        skills=data.get("skills") or [],
        permissions=data.get("permissions") or [],
        runtime=data["runtime"],
        max_concurrent_tasks=data.get("max_concurrent_tasks", 1),
        metadata=data.get("metadata") or {},
    )


def _strip_empty(d: dict) -> dict:
    """Drop None / empty-dict / empty-list / empty-string values for cleaner YAML."""
    result: dict = {}
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, (dict, list, str)) and len(v) == 0:
            continue
        result[k] = v
    return result


def _worker_from_orm(worker: WorkerModel) -> Worker:
    """Convert ORM Worker row to domain Worker dataclass."""
    return Worker(
        id=worker.id,
        name=worker.name,
        role=worker.role,
        status=worker.status,
        runtime=worker.runtime,
        max_concurrent_tasks=worker.max_concurrent_tasks,
        current_profile_version=worker.current_profile_version,
        description=worker.description,
        capabilities=list(worker.capabilities or []),
        permissions=list(worker.permissions or []),
        personality=dict(worker.personality or {}),
        memory_id=worker.memory_id,
        created_at=worker.created_at,
        updated_at=worker.updated_at,
        last_seen_at=worker.last_seen_at,
    )
