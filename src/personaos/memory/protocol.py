"""MemoryAdapter Protocol definition.

Per ADR 0005, ADR 0007, ADR 0025, and ADR 0026:
Memory sits behind a pluggable Protocol interface. Swapping engines
(e.g., In-Memory -> Mem0 -> Graphiti) is pure configuration without
modifying worker execution or kernel logic.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from personaos.memory.scopes import (
    Category,
    DecayPolicy,
    MemoryProvenance,
    MemoryRef,
    Scope,
)


@runtime_checkable
class MemoryAdapter(Protocol):
    """Protocol for pluggable memory engines."""

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
    ) -> MemoryRef:
        """Store a new memory entry."""
        ...

    async def recall(
        self,
        *,
        scope: Scope,
        owner_id: str,
        query: str,
        category: Category | None = None,
        limit: int = 10,
        min_confidence: float | None = None,
        at_time: datetime | None = None,
    ) -> list[MemoryRef]:
        """Retrieve relevant memories matching the query within a specific scope."""
        ...

    async def update(
        self,
        *,
        ref_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        confidence: float | None = None,
    ) -> MemoryRef:
        """Update an existing memory entry."""
        ...

    async def forget(self, *, ref_id: str) -> bool:
        """Delete an existing memory entry."""
        ...

    async def list(
        self,
        *,
        scope: Scope,
        owner_id: str,
        category: Category | None = None,
        limit: int = 50,
        min_confidence: float | None = None,
    ) -> list[MemoryRef]:
        """List memories for a specific scope and owner."""
        ...
