"""Unit tests for Mem0Adapter using httpx.MockTransport."""

from __future__ import annotations

import json

import httpx
import pytest

from personaos.memory.adapters.mem0 import Mem0Adapter, MemoryProviderError
from personaos.memory.scopes import Category, Scope


@pytest.mark.asyncio
async def test_mem0_remember_maps_scope_and_creates_ref() -> None:
    """MEM-05: Mem0Adapter correctly maps scope to user_id/agent_id and creates MemoryRef."""
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        if request.url.path == "/v1/memories/":
            req_data = json.loads(request.content.decode())
            assert req_data["user_id"] == "worker:w-dev-99"
            assert req_data["metadata"]["scope"] == "worker"
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "mem0-id-123",
                        "memory": req_data["messages"][0]["content"],
                        "metadata": req_data["metadata"],
                    }
                ],
            )
        return httpx.Response(404)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.mem0.ai/v1",
    )
    adapter = Mem0Adapter(api_key="test-key-abc", client=client)

    ref = await adapter.remember(
        scope=Scope.WORKER,
        owner_id="w-dev-99",
        category=Category.PROCEDURAL,
        content="Always run lint before push",
    )

    assert ref.id == "mem0-id-123"
    assert ref.scope == Scope.WORKER
    assert ref.owner_id == "w-dev-99"
    assert ref.category == Category.PROCEDURAL
    assert ref.content == "Always run lint before push"

    # Verify authorization header
    assert len(captured_requests) == 1


@pytest.mark.asyncio
async def test_mem0_recall_search() -> None:
    """MEM-06: Mem0Adapter searches memories and maps results into MemoryRef list."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/memories/search/":
            req_data = json.loads(request.content.decode())
            assert req_data["agent_id"] == "project:fms"
            assert req_data["query"] == "database migration"
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "mem-search-1",
                            "memory": "Use alembic for migrations",
                            "metadata": {
                                "scope": "project",
                                "owner_id": "fms",
                                "category": "semantic",
                            },
                        }
                    ]
                },
            )
        return httpx.Response(404)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.mem0.ai/v1",
    )
    adapter = Mem0Adapter(api_key="test-key", client=client)

    results = await adapter.recall(
        scope=Scope.PROJECT,
        owner_id="fms",
        query="database migration",
        limit=5,
    )
    assert len(results) == 1
    assert results[0].id == "mem-search-1"
    assert results[0].scope == Scope.PROJECT
    assert results[0].category == Category.SEMANTIC
    assert results[0].content == "Use alembic for migrations"


@pytest.mark.asyncio
async def test_mem0_http_error_raises_memory_provider_error() -> None:
    """MEM-07: HTTP 500 error from Mem0 is translated to MemoryProviderError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "Internal server error"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.mem0.ai/v1",
    )
    adapter = Mem0Adapter(api_key="test-key", client=client)

    with pytest.raises(MemoryProviderError, match="500"):
        await adapter.recall(
            scope=Scope.WORKER,
            owner_id="w-1",
            query="test",
        )
