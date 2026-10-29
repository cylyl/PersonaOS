"""MemoryService facade — scope validation, isolation enforcement, and safe degradation.

Per ADR 0007, ADR 0025, and ADR 0026:
Enforces per-worker memory privacy boundaries and provides safe degradation
when external memory providers experience outages or latency spikes.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import structlog

from personaos.memory.protocol import MemoryAdapter
from personaos.memory.scopes import (
    Category,
    DecayPolicy,
    MemoryProvenance,
    MemoryRef,
    Scope,
)

logger = structlog.get_logger(__name__)


class MemoryAccessDeniedError(PermissionError):
    """Raised when a worker attempts to access another worker's private memory scope."""


class MemoryConfigurationError(ValueError):
    """Raised when memory adapter configuration is invalid or missing required credentials."""


class MemoryService:
    """Service facade managing memory operations and context assembly."""

    def __init__(
        self,
        adapter: MemoryAdapter,
        *,
        default_limit_per_scope: int = 5,
        default_min_confidence: float = 0.0,
    ) -> None:
        self._adapter = adapter
        self._default_limit_per_scope = default_limit_per_scope
        self._default_min_confidence = default_min_confidence

    @property
    def adapter(self) -> MemoryAdapter:
        """The underlying MemoryAdapter instance."""
        return self._adapter

    def _verify_worker_isolation(
        self,
        scope: Scope,
        owner_id: str,
        caller_worker_id: str | None,
    ) -> None:
        """Ensure a worker cannot query or modify another worker's private memories."""
        if scope == Scope.WORKER and caller_worker_id is not None and owner_id != caller_worker_id:
            raise MemoryAccessDeniedError(
                f"Worker '{caller_worker_id}' is not authorized to access "
                f"private worker memory of '{owner_id}'"
            )

    async def remember(
        self,
        *,
        scope: Scope,
        owner_id: str,
        category: Category,
        content: str,
        metadata: dict[str, Any] | None = None,
        provenance: MemoryProvenance | dict[str, Any] | None = None,
        confidence: float = 1.0,
        decay_policy: DecayPolicy | dict[str, Any] | None = None,
        caller_worker_id: str | None = None,
    ) -> MemoryRef | None:
        """Store a memory entry with isolation enforcement and safe degradation."""
        self._verify_worker_isolation(scope, owner_id, caller_worker_id)
        try:
            return await self._adapter.remember(
                scope=scope,
                owner_id=owner_id,
                category=category,
                content=content,
                metadata=metadata,
                provenance=provenance,
                confidence=confidence,
                decay_policy=decay_policy,
            )
        except Exception as exc:
            logger.warning(
                "memory_remember_degraded",
                scope=scope.value,
                owner_id=owner_id,
                error=str(exc),
            )
            return None

    async def recall(
        self,
        *,
        scope: Scope,
        owner_id: str,
        query: str,
        category: Category | None = None,
        limit: int | None = None,
        min_confidence: float | None = None,
        at_time: datetime | None = None,
        caller_worker_id: str | None = None,
    ) -> list[MemoryRef]:
        """Recall memories with isolation enforcement and safe fallback on errors."""
        self._verify_worker_isolation(scope, owner_id, caller_worker_id)
        effective_limit = limit if limit is not None else self._default_limit_per_scope
        effective_min_conf = (
            min_confidence if min_confidence is not None else self._default_min_confidence
        )
        try:
            return await self._adapter.recall(
                scope=scope,
                owner_id=owner_id,
                query=query,
                category=category,
                limit=effective_limit,
                min_confidence=effective_min_conf,
                at_time=at_time,
            )
        except Exception as exc:
            logger.warning(
                "memory_recall_degraded",
                scope=scope.value,
                owner_id=owner_id,
                error=str(exc),
            )
            return []

    async def recall_five_scopes(
        self,
        *,
        worker_id: str,
        persona_id: str | None,
        project_id: str | None,
        task_id: str,
        query: str,
        limit_per_scope: int | None = None,
        min_confidence: float | None = None,
        at_time: datetime | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Query all five memory scopes concurrently and return structured memory dictionary.

        Scopes queried:
        - worker: private memory for worker_id
        - persona: persona/role knowledge for persona_id or 'default'
        - project: project context for project_id or 'default'
        - task: context for task_id
        - knowledge: shared organizational knowledge
        """
        lim = limit_per_scope if limit_per_scope is not None else self._default_limit_per_scope
        effective_min_conf = (
            min_confidence if min_confidence is not None else self._default_min_confidence
        )
        persona_owner = persona_id or "default"
        project_owner = project_id or "default"

        async def _query_scope(scope: Scope, owner: str) -> tuple[str, list[dict[str, Any]]]:
            refs = await self.recall(
                scope=scope,
                owner_id=owner,
                query=query,
                limit=lim,
                min_confidence=effective_min_conf,
                at_time=at_time,
                caller_worker_id=worker_id,
            )
            return scope.value, [r.to_dict() for r in refs]

        results = await asyncio.gather(
            _query_scope(Scope.WORKER, worker_id),
            _query_scope(Scope.PERSONA, persona_owner),
            _query_scope(Scope.PROJECT, project_owner),
            _query_scope(Scope.TASK, task_id),
            _query_scope(Scope.KNOWLEDGE, "general"),
            return_exceptions=True,
        )

        scoped_memory: dict[str, list[dict[str, Any]]] = {
            "worker": [],
            "persona": [],
            "project": [],
            "task": [],
            "knowledge": [],
        }

        for item in results:
            if isinstance(item, tuple):
                scope_key, entries = item
                scoped_memory[scope_key] = entries
            elif isinstance(item, Exception):
                logger.warning("scoped_memory_gather_error", error=str(item))

        return scoped_memory


def get_default_memory_adapter() -> MemoryAdapter:
    """Instantiate the configured MemoryAdapter based on application settings."""
    from personaos.config import settings
    from personaos.memory.adapters.in_memory import InMemoryMemoryAdapter
    from personaos.memory.adapters.mem0 import Mem0Adapter

    adapter_type = settings.memory_adapter.lower().strip()
    if adapter_type in ("in_memory", "test"):
        return InMemoryMemoryAdapter()

    if adapter_type == "mem0":
        if not settings.mem0_api_key and "api.mem0.ai" in settings.mem0_base_url:
            msg = (
                "PERSONAOS_MEM0_API_KEY is required when PERSONAOS_MEMORY_ADAPTER='mem0' "
                "using public cloud endpoint (https://api.mem0.ai/v1)."
            )
            raise MemoryConfigurationError(msg)
        return Mem0Adapter(
            api_key=settings.mem0_api_key,
            base_url=settings.mem0_base_url,
        )

    raise MemoryConfigurationError(
        f"Unsupported memory adapter '{settings.memory_adapter}'. Must be 'in_memory' or 'mem0'."
    )


def get_default_memory_service(
    adapter: MemoryAdapter | None = None,
) -> MemoryService:
    """Get the default MemoryService instance configured from settings."""
    from personaos.config import settings

    actual_adapter = adapter or get_default_memory_adapter()
    return MemoryService(
        adapter=actual_adapter,
        default_min_confidence=settings.memory_min_confidence,
    )
