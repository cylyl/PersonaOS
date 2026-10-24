# Specification: Worker Memory Subsystem & Five-Scope Context Enrichment

**Module:** `personaos.memory`, `personaos.execution.assembler`, `personaos.execution.context`
**Related ADR:** ADR 0025, ADR 0007, ADR 0005, ADR 0022
**Source of Truth:** `schemas/worker.yaml`, `VIKUNJA.md`, ADR 0007

---

## 1. Memory Architecture & Scope Topology

```
                                  WorkerExecutionContext
                     ┌──────────────────────────────────────────────┐
                     │ worker: Worker (Identity & Role)             │
                     │ profile: WorkerProfile (Versioned Snapshot)  │
                     │ task: Task (Unit of Work)                    │
                     │ upstream_results: dict[str, Any]             │
                     │ recent_experience: list[dict[str, Any]]      │
                     │ memory: dict[str, list[dict[str, Any]]]      │
                     └──────────────────────▲───────────────────────┘
                                            │
                               ExecutionContextAssembler
                                            │
                                      MemoryService
                                            │
                             ┌──────────────┴──────────────┐
                             │                             │
                        Mem0Adapter               InMemoryMemoryAdapter
                   (HTTP to Mem0 REST)               (Isolated Fakes)
                             │                             │
    ┌────────────────────────┼─────────────────────────────┼────────────────────────┐
    ▼                        ▼                             ▼                        ▼
Scope: WORKER            Scope: PERSONA                Scope: PROJECT           Scope: KNOWLEDGE & TASK
(worker_id)              (role / persona)              (project_id)             (task_id / topic)
```

---

## 2. Component Specifications

### 2.1 Scope and Category Definitions (`src/personaos/memory/scopes.py`)

```python
class Scope(StrEnum):
    PERSONA = "persona"
    PROJECT = "project"
    WORKER = "worker"
    TASK = "task"
    KNOWLEDGE = "knowledge"


class Category(StrEnum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    PROFILE = "profile"


@dataclass(frozen=True)
class MemoryRef:
    id: str
    scope: Scope
    owner_id: str
    category: Category
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime | None = None
```

---

### 2.2 `MemoryAdapter` Protocol Contract (`src/personaos/memory/protocol.py`)

```python
@runtime_checkable
class MemoryAdapter(Protocol):
    """Protocol for pluggable memory engines."""

    async def remember(
        self,
        *,
        scope: Scope,
        owner_id: str,
        category: Category,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRef: ...

    async def recall(
        self,
        *,
        scope: Scope,
        owner_id: str,
        query: str,
        category: Category | None = None,
        limit: int = 10,
        at_time: datetime | None = None,
    ) -> list[MemoryRef]: ...

    async def update(
        self,
        *,
        ref_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRef: ...

    async def forget(self, *, ref_id: str) -> bool: ...

    async def list(
        self,
        *,
        scope: Scope,
        owner_id: str,
        category: Category | None = None,
        limit: int = 50,
    ) -> list[MemoryRef]: ...
```

---

### 2.3 Concrete Adapters (`src/personaos/memory/adapters/`)

1. **`Mem0Adapter`:**
   - Interacts with Mem0 REST endpoints:
     - `POST /v1/memories/` (add memory)
     - `POST /v1/memories/search/` (search memories)
     - `GET /v1/memories/{memory_id}/` (get memory)
     - `PUT /v1/memories/{memory_id}/` (update memory)
     - `DELETE /v1/memories/{memory_id}/` (delete memory)
   - Translates `Scope.WORKER` → `user_id = f"worker:{owner_id}"`.
   - Translates `Scope.PERSONA` → `user_id = f"persona:{owner_id}"`.
   - Translates `Scope.PROJECT` → `agent_id = f"project:{owner_id}"`.
   - Translates `Scope.TASK` → `run_id = f"task:{owner_id}"`.
   - Translates `Scope.KNOWLEDGE` → `agent_id = f"knowledge:{owner_id}"`.
   - Timeouts bounded by default to 5.0 seconds.

2. **`InMemoryMemoryAdapter`:**
   - Stores memories in-memory keyed by `ref_id`.
   - Substring case-insensitive search on `content` and scope/category filters.
   - Provides fully deterministic behavior for testing.

---

### 2.4 `MemoryService` (`src/personaos/memory/service.py`)

- Acts as the facade used by `ExecutionContextAssembler`.
- Enforces isolation:
  - If a calling worker requests memories with `scope == Scope.WORKER`, `owner_id` must match `calling_worker_id` (or caller must be an authorized system service).
- Safe degradation:
  - All calls wrapped in try/except. On network or adapter error, logs warning and returns empty results.

---

### 2.5 Context Assembler Integration (`src/personaos/execution/assembler.py`)

- In `enrich(context: WorkerExecutionContext, ...)`:
  - If `memory_service` is available:
    - Runs queries across the 5 scopes with `query = context.task.objective`.
    - Bounds each scope to `limit = 5` items.
    - Appends results into `context.memory: dict[str, list[dict[str, Any]]]`.
    - Converts `MemoryRef` items to serializable dicts: `{"id": ref.id, "scope": ref.scope.value, "category": ref.category.value, "content": ref.content, "metadata": ref.metadata}`.
