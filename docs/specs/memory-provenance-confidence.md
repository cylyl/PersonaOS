# Specification: Memory Provenance, Confidence Scoring, and Decay Policies

**Module:** `personaos.memory.scopes`, `personaos.memory.protocol`, `personaos.memory.service`, `personaos.execution.assembler`
**Related ADR:** ADR 0026, ADR 0025, ADR 0007
**Source of Truth:** `schemas/worker.yaml`, `VIKUNJA.md`

---

## 1. Domain Types (`src/personaos/memory/scopes.py`)

### 1.1 `MemoryProvenance`
```python
@dataclass(frozen=True)
class MemoryProvenance:
    source_type: str = "unknown"  # "task", "execution", "operator", "document", "system", "unknown"
    source_id: str | None = None
    activity: str | None = None
    created_by: str | None = None

    def to_dict(self) -> dict[str, Any]: ...
    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> MemoryProvenance: ...
```

### 1.2 `DecayPolicy`
```python
class DecayStrategy(StrEnum):
    NONE = "none"
    EXPONENTIAL = "exponential"
    LINEAR = "linear"

@dataclass(frozen=True)
class DecayPolicy:
    strategy: DecayStrategy = DecayStrategy.NONE
    half_life_days: float | None = None
    linear_rate_per_day: float | None = None
    min_confidence: float = 0.0

    def calculate(self, initial: float, created_at: datetime, at_time: datetime | None = None) -> float: ...
```

### 1.3 `MemoryRef` Updates
```python
@dataclass(frozen=True)
class MemoryRef:
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
        if self.decay_policy is None:
            return self.confidence
        return self.decay_policy.calculate(self.confidence, self.created_at, at_time)
```

---

## 2. Adapter Protocol (`src/personaos/memory/protocol.py`)

Extend `remember`, `recall`, and `list` signatures:
```python
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
) -> MemoryRef: ...

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
) -> list[MemoryRef]: ...
```

---

## 3. Threshold Filtering in `MemoryService` & `ExecutionContextAssembler`
- `MemoryService.recall` and `recall_five_scopes` evaluate `effective_confidence` against `min_confidence` and drop entries falling below the threshold.
- Defaults to `settings.memory_min_confidence` (default: 0.0).
