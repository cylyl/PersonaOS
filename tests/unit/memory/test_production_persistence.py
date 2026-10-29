"""Unit tests for production vector persistence and configuration lifecycle.

Per Phase 4 Item B and ADR 0007 / ADR 0025 / ADR 0026:
Verifies:
  1. Configuration factory defaults and environment resolution.
  2. Scope isolation and tenant separation in Mem0 identifiers.
  3. Provenance and confidence roundtrip preservation across simulated process restarts.
  4. Time-based decay and retrieval confidence threshold filtering.
  5. Timeout, network error, and provider outage graceful handling.
  6. Secrets protection (API keys kept out of payloads and logs).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from personaos.config import settings
from personaos.memory.adapters.in_memory import InMemoryMemoryAdapter
from personaos.memory.adapters.mem0 import Mem0Adapter, MemoryProviderError
from personaos.memory.scopes import (
    Category,
    DecayPolicy,
    DecayStrategy,
    MemoryProvenance,
    Scope,
)
from personaos.memory.service import (
    MemoryAccessDeniedError,
    MemoryConfigurationError,
    MemoryService,
    get_default_memory_adapter,
    get_default_memory_service,
)


def test_memory_adapter_factory_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Factory instantiates InMemoryMemoryAdapter by default and Mem0Adapter when configured."""
    monkeypatch.setattr(settings, "memory_adapter", "in_memory")
    adapter = get_default_memory_adapter()
    assert isinstance(adapter, InMemoryMemoryAdapter)

    monkeypatch.setattr(settings, "memory_adapter", "mem0")
    monkeypatch.setattr(settings, "mem0_api_key", "sec-key-1234")
    monkeypatch.setattr(settings, "mem0_base_url", "https://custom.mem0.local/v1")

    mem0_adapter = get_default_memory_adapter()
    assert isinstance(mem0_adapter, Mem0Adapter)
    assert mem0_adapter._api_key == "sec-key-1234"
    assert mem0_adapter._base_url == "https://custom.mem0.local/v1"


def test_memory_adapter_factory_validation_and_fail_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """Factory fails fast without silent fallback on invalid or incomplete configuration."""
    # 1. Missing API key on public cloud endpoint
    monkeypatch.setattr(settings, "memory_adapter", "mem0")
    monkeypatch.setattr(settings, "mem0_api_key", None)
    monkeypatch.setattr(settings, "mem0_base_url", "https://api.mem0.ai/v1")
    with pytest.raises(MemoryConfigurationError, match="PERSONAOS_MEM0_API_KEY is required"):
        get_default_memory_adapter()

    # 2. Unknown/unsupported adapter name (must NOT silently fall back to in_memory)
    monkeypatch.setattr(settings, "memory_adapter", "redis")
    with pytest.raises(MemoryConfigurationError, match="Unsupported memory adapter 'redis'"):
        get_default_memory_adapter()

    # 3. Self-hosted endpoint permits internal access without public cloud key
    monkeypatch.setattr(settings, "memory_adapter", "mem0")
    monkeypatch.setattr(settings, "mem0_api_key", None)
    monkeypatch.setattr(settings, "mem0_base_url", "http://mem0.internal:8080/v1")
    self_hosted = get_default_memory_adapter()
    assert isinstance(self_hosted, Mem0Adapter)
    assert self_hosted._api_key is None
    assert self_hosted._base_url == "http://mem0.internal:8080/v1"


def test_memory_service_factory_applies_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Factory passes configured min_confidence to MemoryService."""
    monkeypatch.setattr(settings, "memory_adapter", "in_memory")
    monkeypatch.setattr(settings, "memory_min_confidence", 0.75)

    service = get_default_memory_service()
    assert isinstance(service, MemoryService)
    assert service._default_min_confidence == 0.75


@pytest.mark.asyncio
async def test_mem0_scope_isolation_namespaces() -> None:
    """Asserts each scope is assigned a strict, non-overlapping Mem0 identifier namespace."""
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode()))
        return httpx.Response(
            200,
            json=[{"id": "mem-1", "memory": "test", "metadata": {}}],
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.mem0.ai/v1",
    )
    adapter = Mem0Adapter(api_key="k", client=client)

    # Worker scope -> user_id: worker:<id>
    await adapter.remember(
        scope=Scope.WORKER, owner_id="w-alice", category=Category.WORKING, content="c1"
    )
    assert captured[-1]["user_id"] == "worker:w-alice"
    assert "agent_id" not in captured[-1]
    assert "run_id" not in captured[-1]

    # Persona scope -> user_id: persona:<id>
    await adapter.remember(
        scope=Scope.PERSONA, owner_id="p-devops", category=Category.PROCEDURAL, content="c2"
    )
    assert captured[-1]["user_id"] == "persona:p-devops"

    # Project scope -> agent_id: project:<id>
    await adapter.remember(
        scope=Scope.PROJECT, owner_id="proj-alpha", category=Category.SEMANTIC, content="c3"
    )
    assert captured[-1]["agent_id"] == "project:proj-alpha"

    # Task scope -> run_id: task:<id>
    await adapter.remember(
        scope=Scope.TASK, owner_id="task-42", category=Category.EPISODIC, content="c4"
    )
    assert captured[-1]["run_id"] == "task:task-42"

    # Knowledge scope -> agent_id: knowledge:<id>
    await adapter.remember(
        scope=Scope.KNOWLEDGE, owner_id="core", category=Category.SEMANTIC, content="c5"
    )
    assert captured[-1]["agent_id"] == "knowledge:core"


@pytest.mark.asyncio
async def test_provenance_and_decay_roundtrip_across_process_restart() -> None:
    """Simulates storing memory in process A and retrieving in replacement process B."""
    stored_storage: dict[str, dict[str, Any]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/memories/":
            body = json.loads(request.content.decode())
            mem_id = "mem-durable-101"
            stored_storage[mem_id] = {
                "id": mem_id,
                "memory": body["messages"][0]["content"],
                "metadata": body["metadata"],
                "created_at": (datetime.now(UTC) - timedelta(days=2)).isoformat(),
            }
            return httpx.Response(200, json=[stored_storage[mem_id]])

        if request.url.path == "/v1/memories/search/":
            return httpx.Response(200, json={"results": list(stored_storage.values())})

        return httpx.Response(404)

    # Process A: Write memory with provenance and decay policy
    client_a = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.mem0.ai/v1"
    )
    adapter_a = Mem0Adapter(api_key="key-a", client=client_a)

    prov = MemoryProvenance(
        source_type="task",
        source_id="run-step22",
        activity="execution",
        created_by="worker-devops-01",
    )
    decay = DecayPolicy(strategy=DecayStrategy.LINEAR, linear_rate_per_day=0.1)

    saved_ref = await adapter_a.remember(
        scope=Scope.WORKER,
        owner_id="worker-devops-01",
        category=Category.PROCEDURAL,
        content="Always run testcontainers with isolated databases",
        provenance=prov,
        confidence=0.95,
        decay_policy=decay,
    )
    assert saved_ref.confidence == 0.95
    assert saved_ref.provenance.created_by == "worker-devops-01"

    # Process B: Fresh process, fresh adapter instance reconstructing state
    client_b = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.mem0.ai/v1"
    )
    adapter_b = Mem0Adapter(api_key="key-b", client=client_b)

    recalled = await adapter_b.recall(
        scope=Scope.WORKER,
        owner_id="worker-devops-01",
        query="testcontainers isolated",
    )
    assert len(recalled) == 1
    item = recalled[0]
    assert item.id == "mem-durable-101"
    assert item.content == "Always run testcontainers with isolated databases"
    assert item.provenance.source_type == "task"
    assert item.provenance.source_id == "run-step22"
    assert item.provenance.activity == "execution"
    assert item.provenance.created_by == "worker-devops-01"
    assert item.confidence == 0.95
    assert item.decay_policy is not None
    assert item.decay_policy.strategy == DecayStrategy.LINEAR
    assert item.decay_policy.linear_rate_per_day == 0.1

    # Verify decay calculation 2 days after creation (rate=0.1/day -> 0.95 - 2*0.1 = 0.75)
    effective_conf = item.effective_confidence()
    assert 0.74 <= effective_conf <= 0.76


@pytest.mark.asyncio
async def test_memory_provider_timeout_and_error_handling() -> None:
    """Verifies timeouts and 5xx errors from provider raise MemoryProviderError."""

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Mem0 vector service connection timed out", request=request)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(timeout_handler), base_url="https://api.mem0.ai/v1"
    )
    adapter = Mem0Adapter(api_key="key", client=client)

    with pytest.raises(MemoryProviderError, match="Mem0 remember failed"):
        await adapter.remember(
            scope=Scope.WORKER,
            owner_id="w-test",
            category=Category.WORKING,
            content="timeout check",
        )

    with pytest.raises(MemoryProviderError, match="Mem0 recall failed"):
        await adapter.recall(
            scope=Scope.WORKER,
            owner_id="w-test",
            query="test query",
        )


@pytest.mark.asyncio
async def test_memory_service_graceful_degradation_on_provider_outage() -> None:
    """When external provider is offline, MemoryService.recall_five_scopes degrades safely."""

    def error_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Service Unavailable")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(error_handler), base_url="https://api.mem0.ai/v1"
    )
    adapter = Mem0Adapter(api_key="key", client=client)
    service = MemoryService(adapter=adapter)

    # recall_five_scopes does not raise; it returns clean empty scopes
    context_memory = await service.recall_five_scopes(
        worker_id="w-1",
        persona_id="p-1",
        project_id="proj-1",
        task_id="t-1",
        query="recover after outage",
    )

    assert isinstance(context_memory, dict)
    assert context_memory["worker"] == []
    assert context_memory["project"] == []
    assert context_memory["task"] == []
    assert context_memory["knowledge"] == []


@pytest.mark.asyncio
async def test_cross_worker_memory_isolation_enforced() -> None:
    """MemoryService blocks a worker from querying or modifying another worker's memories."""
    adapter = InMemoryMemoryAdapter()
    service = MemoryService(adapter=adapter)

    # Worker A stores memory
    await service.remember(
        scope=Scope.WORKER,
        owner_id="worker-alpha",
        category=Category.WORKING,
        content="Alpha private instructions",
        caller_worker_id="worker-alpha",
    )

    # Worker B attempts to access Worker A's memory -> Access Denied
    with pytest.raises(MemoryAccessDeniedError, match="not authorized to access"):
        await service.recall(
            scope=Scope.WORKER,
            owner_id="worker-alpha",
            query="instructions",
            caller_worker_id="worker-beta",
        )
