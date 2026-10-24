"""MemoryAdapter Protocol definition.

Per ADR 0005, ADR 0007, and ADR 0025:
Memory sits behind a pluggable Protocol interface. Swapping engines
(e.g., In-Memory -> Mem0 -> Graphiti) is pure configuration without
modifying worker execution or kernel logic.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from personaos.memory.scopes import Category, MemoryRef, Scope


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
    ) -> list[MemoryRef]:
        """List memories for a specific scope and owner."""
        ...
