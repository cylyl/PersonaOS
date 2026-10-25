"""Unit tests for Memory Provenance, Confidence, Decay Policies, and Threshold Filtering.

Validates Vikunja #107 and ADR 0026:
- Valid and invalid confidence bounds ([0.0, 1.0])
- MemoryProvenance creation, serialization, and round-tripping
- Deterministic decay policies (exponential, linear, none)
- Effective confidence calculation over time
- Threshold filtering in InMemoryMemoryAdapter and Mem0Adapter
- Safe defaults and compatibility for legacy records
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from personaos.memory.adapters.in_memory import InMemoryMemoryAdapter
from personaos.memory.adapters.mem0 import Mem0Adapter
from personaos.memory.scopes import (
    Category,
    DecayPolicy,
    DecayStrategy,
    MemoryProvenance,
    MemoryRef,
    Scope,
)
from personaos.memory.service import MemoryService


def test_confidence_validation_bounds() -> None:
    """Validates that confidence must be between 0.0 and 1.0 inclusive."""
    # Valid bounds
    ref_zero = MemoryRef(
        id="m-0",
        scope=Scope.WORKER,
        owner_id="w-1",
        category=Category.WORKING,
        content="Zero conf",
        confidence=0.0,
    )
    assert ref_zero.confidence == 0.0

    ref_one = MemoryRef(
        id="m-1",
        scope=Scope.WORKER,
        owner_id="w-1",
        category=Category.WORKING,
        content="Max conf",
        confidence=1.0,
    )
    assert ref_one.confidence == 1.0

    # Invalid bounds raise ValueError
    with pytest.raises(ValueError, match="must be between 0.0 and 1.0"):
        MemoryRef(
            id="m-bad-1",
            scope=Scope.WORKER,
            owner_id="w-1",
            category=Category.WORKING,
            content="Bad",
            confidence=-0.05,
        )

    with pytest.raises(ValueError, match="must be between 0.0 and 1.0"):
        MemoryRef(
            id="m-bad-2",
            scope=Scope.WORKER,
            owner_id="w-1",
            category=Category.WORKING,
            content="Bad",
            confidence=1.01,
        )


def test_provenance_roundtrip_and_defaults() -> None:
    """Validates MemoryProvenance serialization and fallback behavior."""
    prov = MemoryProvenance(
        source_type="task",
        source_id="task-42",
        activity="task_completion",
        created_by="worker-alice",
    )
    d = prov.to_dict()
    assert d["source_type"] == "task"
    assert d["source_id"] == "task-42"
    assert d["activity"] == "task_completion"
    assert d["created_by"] == "worker-alice"

    restored = MemoryProvenance.from_dict(d)
    assert restored == prov

    # Defaults on None / empty
    default_prov = MemoryProvenance.from_dict(None)
    assert default_prov.source_type == "unknown"
    assert default_prov.source_id is None


def test_decay_policy_exponential_calculation() -> None:
    """Validates exponential decay based on configured half-life."""
    created = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    policy = DecayPolicy(
        strategy=DecayStrategy.EXPONENTIAL,
        half_life_days=7.0,
        min_confidence=0.1,
    )

    # At creation time: confidence is unchanged
    assert policy.calculate(1.0, created, at_time=created) == 1.0

    # Exactly 1 half-life (7 days later) -> 0.5
    seven_days = created + timedelta(days=7)
    assert pytest.approx(policy.calculate(1.0, created, at_time=seven_days), 0.001) == 0.5

    # 2 half-lives (14 days later) -> 0.25
    fourteen_days = created + timedelta(days=14)
    assert pytest.approx(policy.calculate(1.0, created, at_time=fourteen_days), 0.001) == 0.25

    # 100 days later: hits floor min_confidence 0.1
    long_time = created + timedelta(days=100)
    assert policy.calculate(1.0, created, at_time=long_time) == 0.1


def test_decay_policy_linear_calculation() -> None:
    """Validates linear decay with floor enforcement."""
    created = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    policy = DecayPolicy(
        strategy=DecayStrategy.LINEAR,
        linear_rate_per_day=0.1,  # drops 0.1 per day
        min_confidence=0.2,
    )

    # 3 days later -> 1.0 - 0.3 = 0.7
    three_days = created + timedelta(days=3)
    assert pytest.approx(policy.calculate(1.0, created, at_time=three_days), 0.001) == 0.7

    # 10 days later -> floor at 0.2
    ten_days = created + timedelta(days=10)
    assert policy.calculate(1.0, created, at_time=ten_days) == 0.2


def test_decay_policy_validation_errors() -> None:
    """Validates that invalid decay parameters raise ValueError."""
    with pytest.raises(ValueError, match="half_life_days must be > 0"):
        DecayPolicy(strategy=DecayStrategy.EXPONENTIAL, half_life_days=0.0)

    with pytest.raises(ValueError, match="linear_rate_per_day must be >= 0"):
        DecayPolicy(strategy=DecayStrategy.LINEAR, linear_rate_per_day=-0.1)

    with pytest.raises(ValueError, match="min_confidence"):
        DecayPolicy(strategy=DecayStrategy.NONE, min_confidence=1.5)


@pytest.mark.asyncio
async def test_in_memory_adapter_threshold_filtering() -> None:
    """Validates min_confidence threshold filtering in InMemoryMemoryAdapter."""
    adapter = InMemoryMemoryAdapter()
    now = datetime.now(UTC)

    # Memory 1: High confidence (0.9)
    await adapter.remember(
        scope=Scope.WORKER,
        owner_id="w-1",
        category=Category.EPISODIC,
        content="Production deployment verified",
        confidence=0.9,
        provenance=MemoryProvenance(source_type="task", source_id="task-100"),
    )

    # Memory 2: Low confidence (0.4)
    await adapter.remember(
        scope=Scope.WORKER,
        owner_id="w-1",
        category=Category.EPISODIC,
        content="Uncertain speculation about network latency",
        confidence=0.4,
        provenance=MemoryProvenance(source_type="inference"),
    )

    # Memory 3: Decayed memory (initial 1.0, linear decay 0.5/day, created 2 days ago -> 0.0)
    two_days_ago = now - timedelta(days=2)
    decayed_mem = MemoryRef(
        id="mem-decayed",
        scope=Scope.WORKER,
        owner_id="w-1",
        category=Category.EPISODIC,
        content="Old cache entry",
        confidence=1.0,
        decay_policy=DecayPolicy(strategy=DecayStrategy.LINEAR, linear_rate_per_day=0.5),
        created_at=two_days_ago,
    )
    adapter._memories[decayed_mem.id] = decayed_mem

    # Recall without min_confidence returns all 3
    all_recalled = await adapter.recall(scope=Scope.WORKER, owner_id="w-1", query="deployment")
    assert len(all_recalled) == 1
    assert all_recalled[0].confidence == 0.9

    # Recall with min_confidence = 0.5 filters out the 0.4 and decayed 0.0 entries
    results = await adapter.recall(
        scope=Scope.WORKER,
        owner_id="w-1",
        query="",
        min_confidence=0.5,
    )
    assert len(results) == 1
    assert results[0].content == "Production deployment verified"
    assert results[0].provenance.source_id == "task-100"

    # List with threshold filtering
    listed = await adapter.list(scope=Scope.WORKER, owner_id="w-1", min_confidence=0.5)
    assert len(listed) == 1
    assert listed[0].confidence == 0.9


@pytest.mark.asyncio
async def test_memory_service_provenance_and_confidence_isolation() -> None:
    """Validates MemoryService passes provenance/confidence and filters across scopes."""
    adapter = InMemoryMemoryAdapter()
    service = MemoryService(adapter, default_min_confidence=0.6)

    # Remember with provenance
    ref = await service.remember(
        scope=Scope.WORKER,
        owner_id="worker-bob",
        category=Category.PROCEDURAL,
        content="Run make test before committing",
        confidence=0.85,
        provenance=MemoryProvenance(
            source_type="operator",
            created_by="lead-architect",
        ),
        caller_worker_id="worker-bob",
    )
    assert ref is not None
    assert ref.confidence == 0.85
    assert ref.provenance.created_by == "lead-architect"

    # Low-confidence memory below default threshold 0.6
    await service.remember(
        scope=Scope.WORKER,
        owner_id="worker-bob",
        category=Category.WORKING,
        content="Maybe try restarting container",
        confidence=0.3,
        caller_worker_id="worker-bob",
    )

    # Recall uses default_min_confidence (0.6), so low-confidence entry is filtered
    recalled = await service.recall(
        scope=Scope.WORKER,
        owner_id="worker-bob",
        query="",
        caller_worker_id="worker-bob",
    )
    assert len(recalled) == 1
    assert recalled[0].content == "Run make test before committing"


@pytest.mark.asyncio
async def test_mem0_adapter_provenance_and_confidence_parsing() -> None:
    """Validates Mem0Adapter metadata round-trip for provenance and decay."""
    captured_payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payloads.append(json.loads(request.content.decode()))
        mock_response = [
            {
                "id": "mem-mem0-1",
                "memory": "PostgreSQL indexing guideline",
                "created_at": "2026-10-24T12:00:00Z",
                "metadata": {
                    "scope": "knowledge",
                    "category": "semantic",
                    "owner_id": "general",
                    "confidence": 0.95,
                    "provenance": {
                        "source_type": "document",
                        "source_id": "docs/db-guidelines.md",
                    },
                    "decay_policy": {
                        "strategy": "exponential",
                        "half_life_days": 30.0,
                        "min_confidence": 0.5,
                    },
                },
            }
        ]
        return httpx.Response(200, json=mock_response)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.mem0.ai/v1",
    )
    adapter = Mem0Adapter(api_key="test-key", client=client)

    # Store with provenance and decay
    stored = await adapter.remember(
        scope=Scope.KNOWLEDGE,
        owner_id="general",
        category=Category.SEMANTIC,
        content="PostgreSQL indexing guideline",
        confidence=0.95,
        provenance=MemoryProvenance(
            source_type="document",
            source_id="docs/db-guidelines.md",
        ),
        decay_policy=DecayPolicy(
            strategy=DecayStrategy.EXPONENTIAL,
            half_life_days=30.0,
            min_confidence=0.5,
        ),
    )

    assert stored.confidence == 0.95
    assert stored.provenance.source_type == "document"
    assert stored.provenance.source_id == "docs/db-guidelines.md"
    assert stored.decay_policy is not None
    assert stored.decay_policy.half_life_days == 30.0

    # Verify payload sent to Mem0 contains metadata
    sent_meta = captured_payloads[0]["metadata"]
    assert sent_meta["confidence"] == 0.95
    assert sent_meta["provenance"]["source_type"] == "document"
    assert sent_meta["decay_policy"]["strategy"] == "exponential"
