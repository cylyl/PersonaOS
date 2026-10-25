"""Mem0 REST API adapter for the MemoryAdapter protocol.

Per ADR 0007, ADR 0025, and ADR 0026:
Provides seamless communication with Mem0 managed API or self-hosted Mem0 instance
via HTTP REST endpoints without requiring third-party library runtime lock-in.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
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


class MemoryProviderError(Exception):
    """Raised when an external memory provider fails."""


class Mem0Adapter(MemoryAdapter):
    """HTTP client adapter communicating with the Mem0 REST API."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.mem0.ai/v1",
        timeout_seconds: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._client = client
        self._owns_client = client is None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Token {self._api_key}"
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=self._timeout_seconds,
        )
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client if owned."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _map_scope_params(self, scope: Scope, owner_id: str) -> dict[str, str]:
        """Map PersonaOS scope and owner_id into Mem0 identifier parameters."""
        match scope:
            case Scope.WORKER:
                return {"user_id": f"worker:{owner_id}"}
            case Scope.PERSONA:
                return {"user_id": f"persona:{owner_id}"}
            case Scope.PROJECT:
                return {"agent_id": f"project:{owner_id}"}
            case Scope.TASK:
                return {"run_id": f"task:{owner_id}"}
            case Scope.KNOWLEDGE:
                return {"agent_id": f"knowledge:{owner_id}"}

    def _parse_entry(
        self,
        item: dict[str, Any],
        scope: Scope,
        owner_id: str,
        category: Category,
    ) -> MemoryRef:
        """Parse a Mem0 raw memory item into a domain MemoryRef."""
        ref_id = str(item.get("id") or item.get("memory_id") or uuid.uuid4())
        content = str(item.get("memory") or item.get("text") or item.get("content") or "")
        meta = dict(item.get("metadata") or {})

        # Extract parsed scope/category from metadata if available
        item_scope_str = meta.get("scope", scope.value)
        item_cat_str = meta.get("category", category.value)
        try:
            entry_scope = Scope(item_scope_str)
        except ValueError:
            entry_scope = scope
        try:
            entry_cat = Category(item_cat_str)
        except ValueError:
            entry_cat = category

        # Extract provenance, confidence, and decay from metadata with graceful defaults
        prov_raw = meta.get("provenance")
        prov = MemoryProvenance.from_dict(prov_raw if isinstance(prov_raw, dict) else None)

        try:
            raw_conf = float(meta.get("confidence", 1.0))
            conf = max(0.0, min(1.0, raw_conf))
        except (ValueError, TypeError):
            conf = 1.0

        decay_raw = meta.get("decay_policy")
        decay = DecayPolicy.from_dict(decay_raw if isinstance(decay_raw, dict) else None)

        created_at_raw = item.get("created_at")
        created_at = datetime.now(UTC)
        if created_at_raw:
            with contextlib.suppress(Exception):
                created_at = datetime.fromisoformat(str(created_at_raw).replace("Z", "+00:00"))

        return MemoryRef(
            id=ref_id,
            scope=entry_scope,
            owner_id=meta.get("owner_id", owner_id),
            category=entry_cat,
            content=content,
            metadata=meta,
            provenance=prov,
            confidence=conf,
            decay_policy=decay,
            created_at=created_at,
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
    ) -> MemoryRef:
        """Store a new memory in Mem0."""
        client = self._get_client()
        scope_params = self._map_scope_params(scope, owner_id)

        prov = (
            provenance
            if isinstance(provenance, MemoryProvenance)
            else MemoryProvenance.from_dict(provenance)
        )
        decay = (
            decay_policy
            if isinstance(decay_policy, DecayPolicy)
            else DecayPolicy.from_dict(decay_policy)
        )

        full_meta = {
            **(metadata or {}),
            "scope": scope.value,
            "category": category.value,
            "owner_id": owner_id,
            "provenance": prov.to_dict(),
            "confidence": confidence,
            "decay_policy": decay.to_dict() if decay else None,
        }

        payload: dict[str, Any] = {
            "messages": [{"role": "user", "content": content}],
            "metadata": full_meta,
            **scope_params,
        }

        try:
            response = await client.post("/memories/", json=payload)
            response.raise_for_status()
            data = response.json()
            # Mem0 add returns a list of created memories or a single object
            if isinstance(data, list) and data:
                return self._parse_entry(data[0], scope, owner_id, category)
            if isinstance(data, dict):
                return self._parse_entry(data, scope, owner_id, category)
            # Fallback if empty response
            return MemoryRef(
                id=f"mem-{uuid.uuid4()}",
                scope=scope,
                owner_id=owner_id,
                category=category,
                content=content,
                metadata=dict(metadata or {}),
                provenance=prov,
                confidence=confidence,
                decay_policy=decay,
            )
        except httpx.HTTPError as exc:
            logger.warning("mem0_remember_failed", error=str(exc), scope=scope.value)
            raise MemoryProviderError(f"Mem0 remember failed: {exc}") from exc

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
        """Search memories in Mem0."""
        client = self._get_client()
        scope_params = self._map_scope_params(scope, owner_id)
        payload: dict[str, Any] = {
            "query": query,
            "limit": limit,
            **scope_params,
        }

        try:
            response = await client.post("/memories/search/", json=payload)
            response.raise_for_status()
            data = response.json()
            items = data if isinstance(data, list) else data.get("results", [])
            default_cat = category or Category.WORKING
            refs = [self._parse_entry(item, scope, owner_id, default_cat) for item in items]
            if category is not None:
                refs = [r for r in refs if r.category == category]
            if min_confidence is not None:
                refs = [r for r in refs if r.effective_confidence(at_time) >= min_confidence]
            refs.sort(
                key=lambda m: (m.effective_confidence(at_time), m.created_at),
                reverse=True,
            )
            return refs[:limit]
        except httpx.HTTPError as exc:
            logger.warning("mem0_recall_failed", error=str(exc), scope=scope.value)
            raise MemoryProviderError(f"Mem0 recall failed: {exc}") from exc

    async def update(
        self,
        *,
        ref_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        confidence: float | None = None,
    ) -> MemoryRef:
        """Update an existing memory in Mem0."""
        client = self._get_client()
        payload: dict[str, Any] = {"text": content}
        meta_update = dict(metadata or {})
        if confidence is not None:
            meta_update["confidence"] = confidence
        if meta_update:
            payload["metadata"] = meta_update

        try:
            response = await client.put(f"/memories/{ref_id}/", json=payload)
            response.raise_for_status()
            data = response.json()
            return self._parse_entry(
                data if isinstance(data, dict) else {},
                Scope.WORKER,
                "unknown",
                Category.WORKING,
            )
        except httpx.HTTPError as exc:
            logger.warning("mem0_update_failed", ref_id=ref_id, error=str(exc))
            raise MemoryProviderError(f"Mem0 update failed: {exc}") from exc

    async def forget(self, *, ref_id: str) -> bool:
        """Delete an existing memory from Mem0."""
        client = self._get_client()
        try:
            response = await client.delete(f"/memories/{ref_id}/")
            response.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            logger.warning("mem0_forget_failed", ref_id=ref_id, error=str(exc))
            raise MemoryProviderError(f"Mem0 forget failed: {exc}") from exc

    async def list(
        self,
        *,
        scope: Scope,
        owner_id: str,
        category: Category | None = None,
        limit: int = 50,
        min_confidence: float | None = None,
    ) -> list[MemoryRef]:
        """List memories matching scope and owner from Mem0."""
        client = self._get_client()
        params = self._map_scope_params(scope, owner_id)
        params["limit"] = str(limit)

        try:
            response = await client.get("/memories/", params=params)
            response.raise_for_status()
            data = response.json()
            items = data if isinstance(data, list) else data.get("results", [])
            default_cat = category or Category.WORKING
            refs = [self._parse_entry(item, scope, owner_id, default_cat) for item in items]
            if category is not None:
                refs = [r for r in refs if r.category == category]
            if min_confidence is not None:
                refs = [r for r in refs if r.effective_confidence() >= min_confidence]
            refs.sort(
                key=lambda m: (m.effective_confidence(), m.created_at),
                reverse=True,
            )
            return refs[:limit]
        except httpx.HTTPError as exc:
            logger.warning("mem0_list_failed", error=str(exc), scope=scope.value)
            raise MemoryProviderError(f"Mem0 list failed: {exc}") from exc
