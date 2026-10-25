"""Memory scopes, category enumerations, provenance, and confidence models for PersonaOS.

Per ADR 0007, ADR 0025, and ADR 0026:
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


class DecayStrategy(StrEnum):
    """Decay strategy for memory confidence over time."""

    NONE = "none"
    EXPONENTIAL = "exponential"
    LINEAR = "linear"


@dataclass(frozen=True)
class MemoryProvenance:
    """Explicit provenance metadata tracking the origin of a memory entry."""

    source_type: str = "unknown"  # "task", "execution", "operator", "document", "system", "unknown"
    source_id: str | None = None
    activity: str | None = None
    created_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert provenance to a serializable dictionary."""
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "activity": self.activity,
            "created_by": self.created_by,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> MemoryProvenance:
        """Parse dictionary into MemoryProvenance with safe defaults."""
        if not data:
            return cls()
        return cls(
            source_type=str(data.get("source_type") or "unknown"),
            source_id=data.get("source_id"),
            activity=data.get("activity"),
            created_by=data.get("created_by"),
        )


@dataclass(frozen=True)
class DecayPolicy:
    """Policy controlling how confidence score decays over time."""

    strategy: DecayStrategy = DecayStrategy.NONE
    half_life_days: float | None = None
    linear_rate_per_day: float | None = None
    min_confidence: float = 0.0

    def __post_init__(self) -> None:
        if not (0.0 <= self.min_confidence <= 1.0):
            raise ValueError(f"min_confidence {self.min_confidence} must be between 0.0 and 1.0")
        if self.strategy == DecayStrategy.EXPONENTIAL and (
            self.half_life_days is None or self.half_life_days <= 0
        ):
            raise ValueError("half_life_days must be > 0 for exponential decay")
        if self.strategy == DecayStrategy.LINEAR and (
            self.linear_rate_per_day is None or self.linear_rate_per_day < 0
        ):
            raise ValueError("linear_rate_per_day must be >= 0 for linear decay")

    def calculate(
        self,
        initial: float,
        created_at: datetime,
        at_time: datetime | None = None,
    ) -> float:
        """Deterministically calculate decayed confidence at given time."""
        if self.strategy == DecayStrategy.NONE:
            return initial
        target_time = at_time or datetime.now(UTC)
        if target_time <= created_at:
            return initial

        elapsed_seconds = (target_time - created_at).total_seconds()
        elapsed_days = max(0.0, elapsed_seconds / 86400.0)

        if self.strategy == DecayStrategy.EXPONENTIAL:
            assert self.half_life_days is not None
            decayed = initial * (0.5 ** (elapsed_days / self.half_life_days))
        elif self.strategy == DecayStrategy.LINEAR:
            assert self.linear_rate_per_day is not None
            decayed = initial - (self.linear_rate_per_day * elapsed_days)
        else:
            decayed = initial

        return float(max(self.min_confidence, min(1.0, float(decayed))))

    def to_dict(self) -> dict[str, Any]:
        """Convert decay policy to serializable dictionary."""
        return {
            "strategy": self.strategy.value,
            "half_life_days": self.half_life_days,
            "linear_rate_per_day": self.linear_rate_per_day,
            "min_confidence": self.min_confidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> DecayPolicy | None:
        """Parse dictionary into DecayPolicy with safe defaults."""
        if not data:
            return None
        strat_val = data.get("strategy")
        if not strat_val or strat_val == DecayStrategy.NONE.value:
            return None
        return cls(
            strategy=DecayStrategy(strat_val),
            half_life_days=(
                float(data["half_life_days"]) if data.get("half_life_days") is not None else None
            ),
            linear_rate_per_day=(
                float(data["linear_rate_per_day"])
                if data.get("linear_rate_per_day") is not None
                else None
            ),
            min_confidence=float(data.get("min_confidence", 0.0)),
        )


@dataclass(frozen=True)
class MemoryRef:
    """Immutable reference to an individual memory entry with provenance and confidence."""

    id: str
    scope: Scope
    owner_id: str
    category: Category
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    provenance: MemoryProvenance = field(default_factory=MemoryProvenance)
    confidence: float = 1.0
    decay_policy: DecayPolicy | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"Confidence score {self.confidence} must be between 0.0 and 1.0")

    def effective_confidence(self, at_time: datetime | None = None) -> float:
        """Calculate the confidence score after applying any configured decay policy."""
        if self.decay_policy is None:
            return self.confidence
        return self.decay_policy.calculate(self.confidence, self.created_at, at_time)

    def to_dict(self) -> dict[str, Any]:
        """Convert memory entry to a serializable dictionary."""
        return {
            "id": self.id,
            "scope": self.scope.value,
            "owner_id": self.owner_id,
            "category": self.category.value,
            "content": self.content,
            "metadata": dict(self.metadata),
            "provenance": self.provenance.to_dict(),
            "confidence": self.confidence,
            "effective_confidence": self.effective_confidence(),
            "decay_policy": self.decay_policy.to_dict() if self.decay_policy else None,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
