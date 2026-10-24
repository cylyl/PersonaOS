"""In-memory reference implementation of MemoryAdapter.

Provides deterministic, isolated memory operations for unit and integration testing
without requiring external network services or API keys.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from personaos.memory.protocol import MemoryAdapter
from personaos.memory.scopes import Category, MemoryRef, Scope


class InMemoryMemoryAdapter(MemoryAdapter):
    """In-memory thread-safe implementation of MemoryAdapter."""

    def __init__(self) -> None:
        self._memories: dict[str, MemoryRef] = {}
        self._lock = asyncio.Lock()

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
        ref_id = f"mem-{uuid.uuid4()}"
        now = datetime.now(UTC)
        ref = MemoryRef(
            id=ref_id,
            scope=scope,
            owner_id=owner_id,
            category=category,
            content=content,
            metadata=dict(metadata or {}),
            created_at=now,
        )
        async with self._lock:
            self._memories[ref_id] = ref
        return ref

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
        """Recall memories matching the scope and containing search query terms."""
        terms = [t.lower() for t in query.split() if t.strip()]
        matches: list[MemoryRef] = []

        async with self._lock:
            for ref in self._memories.values():
                if ref.scope != scope or ref.owner_id != owner_id:
                    continue
                if category is not None and ref.category != category:
                    continue
                if at_time is not None and ref.created_at > at_time:
                    continue

                # Content relevance check (keyword / substring match)
                content_lower = ref.content.lower()
                if not terms or any(t in content_lower for t in terms):
                    matches.append(ref)

        # Sort newest first, up to limit
        matches.sort(key=lambda m: m.created_at, reverse=True)
        return matches[:limit]

    async def update(
        self,
        *,
        ref_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRef:
        """Update an existing memory entry."""
        async with self._lock:
            existing = self._memories.get(ref_id)
            if existing is None:
                raise KeyError(f"MemoryRef with id {ref_id} not found")

            updated_meta = dict(existing.metadata)
            if metadata:
                updated_meta.update(metadata)

            updated = MemoryRef(
                id=existing.id,
                scope=existing.scope,
                owner_id=existing.owner_id,
                category=existing.category,
                content=content,
                metadata=updated_meta,
                created_at=existing.created_at,
                updated_at=datetime.now(UTC),
            )
            self._memories[ref_id] = updated
            return updated

    async def forget(self, *, ref_id: str) -> bool:
        """Delete an existing memory entry."""
        async with self._lock:
            return self._memories.pop(ref_id, None) is not None

    async def list(
        self,
        *,
        scope: Scope,
        owner_id: str,
        category: Category | None = None,
        limit: int = 50,
    ) -> list[MemoryRef]:
        """List memories matching scope and owner."""
        results: list[MemoryRef] = []
        async with self._lock:
            for ref in self._memories.values():
                if (
                    ref.scope == scope
                    and ref.owner_id == owner_id
                    and (category is None or ref.category == category)
                ):
                    results.append(ref)
        results.sort(key=lambda m: m.created_at, reverse=True)
        return results[:limit]
