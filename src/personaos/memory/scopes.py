"""Memory scopes and category enumerations for PersonaOS.

Per ADR 0007 and ADR 0025:
Memory is scoped, not global. Five scopes:
  - persona: preferences, working style, long-term goals
  - project: per-project memory (architecture, decisions, constraints)
  - worker: per-worker persistent experience and skills
  - task: current, completed, or failed task context
  - knowledge: reusable domain knowledge and documentation
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class Scope(StrEnum):
    """Memory scope partition."""

    PERSONA = "persona"
    PROJECT = "project"
    WORKER = "worker"
    TASK = "task"
    KNOWLEDGE = "knowledge"


class Category(StrEnum):
    """Memory category classification."""

    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    PROFILE = "profile"


@dataclass(frozen=True)
class MemoryRef:
    """Immutable reference to an individual memory entry."""

    id: str
    scope: Scope
    owner_id: str
    category: Category
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert memory entry to a serializable dictionary."""
        return {
            "id": self.id,
            "scope": self.scope.value,
            "owner_id": self.owner_id,
            "category": self.category.value,
            "content": self.content,
            "metadata": dict(self.metadata),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
