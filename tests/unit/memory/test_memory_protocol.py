"""Unit tests for MemoryAdapter protocol and InMemoryMemoryAdapter."""

from __future__ import annotations

import pytest

from personaos.memory.adapters.in_memory import InMemoryMemoryAdapter
from personaos.memory.protocol import MemoryAdapter
from personaos.memory.scopes import Category, MemoryRef, Scope


def test_in_memory_implements_protocol() -> None:
    """MEM-01: InMemoryMemoryAdapter adheres to the runtime-checkable MemoryAdapter Protocol."""
    adapter = InMemoryMemoryAdapter()
    assert isinstance(adapter, MemoryAdapter)


@pytest.mark.asyncio
async def test_remember_and_recall_basic() -> None:
    """MEM-02: Stores and recalls memory entries filtered by scope and query."""
    adapter = InMemoryMemoryAdapter()

    ref1 = await adapter.remember(
        scope=Scope.WORKER,
        owner_id="w-dev-01",
        category=Category.PROCEDURAL,
        content="Use Helm chart v2 for staging deployments",
        metadata={"service": "frontend"},
    )
    assert isinstance(ref1, MemoryRef)
    assert ref1.scope == Scope.WORKER
    assert ref1.owner_id == "w-dev-01"
    assert ref1.category == Category.PROCEDURAL
    assert "Helm" in ref1.content

    # Another memory in different scope
    await adapter.remember(
        scope=Scope.PROJECT,
        owner_id="proj-fms",
        category=Category.SEMANTIC,
        content="PostgreSQL 16 is required for all relational data",
    )

    # Recall matching query in Scope.WORKER
    results = await adapter.recall(
        scope=Scope.WORKER,
        owner_id="w-dev-01",
        query="Helm chart",
    )
    assert len(results) == 1
    assert results[0].id == ref1.id

    # Scope isolation: query in Scope.PROJECT does not find worker memory
    proj_results = await adapter.recall(
        scope=Scope.PROJECT,
        owner_id="proj-fms",
        query="Helm chart",
    )
    assert len(proj_results) == 0

    # Recall with non-matching query returns empty list
    empty_results = await adapter.recall(
        scope=Scope.WORKER,
        owner_id="w-dev-01",
        query="unrelated keyword xyz",
    )
    assert len(empty_results) == 0


@pytest.mark.asyncio
async def test_category_filtering_and_limits() -> None:
    """MEM-03: Category filtering and limit bounding work as specified."""
    adapter = InMemoryMemoryAdapter()

    for i in range(5):
        await adapter.remember(
            scope=Scope.WORKER,
            owner_id="w-dev-02",
            category=Category.EPISODIC,
            content=f"Encountered timeout on deploy step {i}",
        )
    await adapter.remember(
        scope=Scope.WORKER,
        owner_id="w-dev-02",
        category=Category.PROCEDURAL,
        content="Always run smoke test after deploy",
    )

    # Filter by Category.EPISODIC with limit=2
    episodic = await adapter.recall(
        scope=Scope.WORKER,
        owner_id="w-dev-02",
        query="deploy",
        category=Category.EPISODIC,
        limit=2,
    )
    assert len(episodic) == 2
    assert all(r.category == Category.EPISODIC for r in episodic)

    # Filter by Category.PROCEDURAL
    procedural = await adapter.recall(
        scope=Scope.WORKER,
        owner_id="w-dev-02",
        query="deploy",
        category=Category.PROCEDURAL,
    )
    assert len(procedural) == 1
    assert procedural[0].category == Category.PROCEDURAL


@pytest.mark.asyncio
async def test_update_and_forget() -> None:
    """MEM-04: Updates and deletes memory entries cleanly."""
    adapter = InMemoryMemoryAdapter()

    ref = await adapter.remember(
        scope=Scope.KNOWLEDGE,
        owner_id="general",
        category=Category.SEMANTIC,
        content="Antigravity v1 convention",
    )

    # Update
    updated = await adapter.update(
        ref_id=ref.id,
        content="Antigravity v2 convention with new protocol",
        metadata={"version": 2},
    )
    assert updated.content == "Antigravity v2 convention with new protocol"
    assert updated.metadata.get("version") == 2
    assert updated.updated_at is not None

    # Verify updated content recalled
    recalled = await adapter.recall(scope=Scope.KNOWLEDGE, owner_id="general", query="convention")
    assert len(recalled) == 1
    assert recalled[0].content == "Antigravity v2 convention with new protocol"

    # Forget
    deleted = await adapter.forget(ref_id=ref.id)
    assert deleted is True

    # Confirm forgotten
    recalled_after = await adapter.recall(
        scope=Scope.KNOWLEDGE, owner_id="general", query="convention"
    )
    assert len(recalled_after) == 0

    # Forgetting non-existent id returns False
    assert await adapter.forget(ref_id="non-existent") is False
