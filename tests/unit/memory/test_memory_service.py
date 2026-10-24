"""Unit tests for MemoryService: isolation enforcement, safe degradation, and five-scope recall."""

from __future__ import annotations

import pytest

from personaos.memory.adapters.in_memory import InMemoryMemoryAdapter
from personaos.memory.adapters.mem0 import MemoryProviderError
from personaos.memory.protocol import MemoryAdapter
from personaos.memory.scopes import Category, Scope
from personaos.memory.service import MemoryAccessDeniedError, MemoryService


@pytest.mark.asyncio
async def test_cross_worker_isolation_enforced() -> None:
    """MEM-08: Querying another worker's private Scope.WORKER raises MemoryAccessDeniedError."""
    adapter = InMemoryMemoryAdapter()
    service = MemoryService(adapter)

    await service.remember(
        scope=Scope.WORKER,
        owner_id="worker-alice",
        category=Category.PROCEDURAL,
        content="Alice's private secret note",
        caller_worker_id="worker-alice",
    )

    # Bob tries to access Alice's worker memory -> denied
    with pytest.raises(MemoryAccessDeniedError, match="not authorized"):
        await service.recall(
            scope=Scope.WORKER,
            owner_id="worker-alice",
            query="secret",
            caller_worker_id="worker-bob",
        )

    # Alice querying her own worker memory -> permitted
    alice_mem = await service.recall(
        scope=Scope.WORKER,
        owner_id="worker-alice",
        query="secret",
        caller_worker_id="worker-alice",
    )
    assert len(alice_mem) == 1
    assert alice_mem[0].content == "Alice's private secret note"


@pytest.mark.asyncio
async def test_safe_degradation_on_adapter_failure() -> None:
    """MEM-09: Adapter failures/timeouts degrade safely to empty list without crashing."""

    class FailingAdapter(MemoryAdapter):
        async def remember(self, **kwargs):
            raise MemoryProviderError("Connection refused to memory service")

        async def recall(self, **kwargs):
            raise MemoryProviderError("Read timeout from memory service")

        async def update(self, **kwargs):
            raise MemoryProviderError("Fail")

        async def forget(self, **kwargs):
            raise MemoryProviderError("Fail")

        async def list(self, **kwargs):
            raise MemoryProviderError("Fail")

    service = MemoryService(FailingAdapter())

    # Recall returns empty list instead of raising
    recalled = await service.recall(
        scope=Scope.PROJECT,
        owner_id="proj-1",
        query="anything",
    )
    assert recalled == []

    # Remember returns None instead of raising
    remembered = await service.remember(
        scope=Scope.PROJECT,
        owner_id="proj-1",
        category=Category.SEMANTIC,
        content="Important fact",
    )
    assert remembered is None


@pytest.mark.asyncio
async def test_recall_five_scopes() -> None:
    """MEM-10: Queries all five scopes concurrently and returns structured scoped dictionary."""
    adapter = InMemoryMemoryAdapter()
    service = MemoryService(adapter)

    # Seed memories in all 5 scopes
    await adapter.remember(
        scope=Scope.WORKER,
        owner_id="w-dev-01",
        category=Category.EPISODIC,
        content="Worker experience on k8s cluster setup",
    )
    await adapter.remember(
        scope=Scope.PERSONA,
        owner_id="devops",
        category=Category.PROFILE,
        content="Persona preference: favors k8s declarative infrastructure",
    )
    await adapter.remember(
        scope=Scope.PROJECT,
        owner_id="project-phoenix",
        category=Category.SEMANTIC,
        content="Phoenix project uses Terraform and k8s",
    )
    await adapter.remember(
        scope=Scope.TASK,
        owner_id="task-42",
        category=Category.WORKING,
        content="Current task step: provision k8s cluster namespace",
    )
    await adapter.remember(
        scope=Scope.KNOWLEDGE,
        owner_id="general",
        category=Category.PROCEDURAL,
        content="Organization policy: k8s clusters require RBAC",
    )

    scoped = await service.recall_five_scopes(
        worker_id="w-dev-01",
        persona_id="devops",
        project_id="project-phoenix",
        task_id="task-42",
        query="k8s",
    )

    assert "worker" in scoped
    assert len(scoped["worker"]) == 1
    assert "persona" in scoped
    assert len(scoped["persona"]) == 1
    assert "project" in scoped
    assert len(scoped["project"]) == 1
    assert "task" in scoped
    assert len(scoped["task"]) == 1
    assert "knowledge" in scoped
    assert len(scoped["knowledge"]) == 1

    assert "cluster setup" in scoped["worker"][0]["content"]
    assert "declarative" in scoped["persona"][0]["content"]
    assert "Terraform" in scoped["project"][0]["content"]
    assert "namespace" in scoped["task"][0]["content"]
    assert "RBAC" in scoped["knowledge"][0]["content"]
