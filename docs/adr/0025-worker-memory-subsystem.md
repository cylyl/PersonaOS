# ADR 0025: Worker Memory Subsystem & Five-Scope Context Enrichment

**Status:** Accepted (Step 22 Implementation Phase)
**Date:** 2026-10-23
**Deciders:** PersonaOS Architecture Team
**Consulted:** Execution Kernel, Context Assembler, Security Policy Engine
**Informed:** Operators, Platform Engineers

---

## Context & Problem Statement

PersonaOS operates on the founding thesis: **“Compute is replaceable; the worker is persistent.”**
While Steps 18 through 21 established worker persistence across compute failures, node supervision, and context assembly, workers remained amnesiac between unrelated tasks and sessions without a durable memory layer.

Per ADR 0007, memory is scoped by category and sits behind a `MemoryAdapter` Protocol, avoiding direct coupling to any proprietary memory service. The roadmap defines five memory scopes:
1. `persona` — persistent persona-level traits, working style, and preferences.
2. `project` — project-level architecture, decisions, and constraints.
3. `worker` — persistent worker experience, learned lessons, and domain skills.
4. `task` — task-specific ephemeral context and intermediate results.
5. `knowledge` — reusable organization-wide knowledge and documentation.

Step 22 implements the Phase 2 Worker Memory Subsystem:
- A typed, provider-agnostic `MemoryAdapter` Protocol.
- An HTTP-based `Mem0Adapter` for the Mem0 service.
- An `InMemoryMemoryAdapter` for deterministic local development and isolated testing.
- A `MemoryService` providing validation, privacy boundary enforcement, and failure isolation.
- Integration into `ExecutionContextAssembler` to enrich `WorkerExecutionContext` across all five scopes before runtime execution.

---

## Decision

We will implement the **Worker Memory Subsystem** with the following architectural components:

### 1. `MemoryAdapter` Protocol & Domain Models
In `src/personaos/memory/`:
- **Scopes:** `Scope` enum (`persona`, `project`, `worker`, `task`, `knowledge`).
- **Categories:** `Category` enum (`working`, `episodic`, `semantic`, `procedural`, `profile`).
- **`MemoryRef` Dataclass:**
  - `id: str`
  - `scope: Scope`
  - `owner_id: str`
  - `category: Category`
  - `content: str`
  - `metadata: dict[str, Any]`
  - `created_at: datetime`
  - `updated_at: datetime | None`
- **Protocol Contract (`MemoryAdapter`):**
  - `async def remember(...) -> MemoryRef`
  - `async def recall(...) -> list[MemoryRef]`
  - `async def update(...) -> MemoryRef`
  - `async def forget(...) -> None`
  - `async def list(...) -> list[MemoryRef]`

### 2. Concrete Memory Adapters
- **`Mem0Adapter` (`src/personaos/memory/adapters/mem0.py`):**
  - Communicates via `httpx.AsyncClient` with Mem0 REST API (`api.mem0.ai/v1` or self-hosted).
  - API credentials configured via `settings.mem0_api_key` / `settings.mem0_base_url`.
  - Maps PersonaOS `(scope, owner_id)` into Mem0 identifiers (`user_id`, `agent_id`, `run_id`, `metadata`).
  - Handles rate limits, timeouts, and network errors gracefully without crashing the caller.
- **`InMemoryMemoryAdapter` (`src/personaos/memory/adapters/in_memory.py`):**
  - Pure Python in-memory store supporting full CRUD and semantic/keyword substring matching.
  - Zero external network dependencies, ensuring reproducible, deterministic test runs.

### 3. `MemoryService` Boundary & Isolation
In `src/personaos/memory/service.py`:
- Enforces cross-worker privacy: queries for `Scope.WORKER` require `owner_id == calling_worker_id`. A worker cannot query another worker's private memories.
- Safe degradation: If the underlying adapter raises a timeout or connection error, `MemoryService` logs a warning and returns an empty list, allowing kernel execution to proceed without failing the task.

### 4. Context Enrichment Pipeline Integration
In `src/personaos/execution/assembler.py` and `src/personaos/execution/context.py`:
- `WorkerExecutionContext` extended with `memory: dict[str, list[dict[str, Any]]]`.
- `ExecutionContextAssembler.enrich()` queries the five scopes when `MemoryService` is present:
  - `worker`: scoped to `context.worker.id`.
  - `persona`: scoped to `context.worker.role` or persona identity.
  - `project`: scoped to `context.task.input.get("project_id", "default")`.
  - `task`: scoped to `context.task.id`.
  - `knowledge`: scoped to general knowledge domains.
- **Security Invariant:** Memory data is treated as untrusted external context. It cannot override security policies, grant permissions, alter tool policies, or modify kernel task states.

---

## Consequences

### Positive
- Workers persist knowledge and experience across compute replacements and sessions.
- Clean Protocol boundary allows swapping Mem0 for Graphiti or pgvector without touching worker or kernel logic.
- Strong cross-worker memory isolation prevents information leakage between tenants and workers.
- Fail-safe design ensures memory service outages do not bring down task execution.

### Negative / Trade-offs
- Memory retrieval adds a bounded latency overhead to context assembly (mitigated by asynchronous I/O and query limits).
- Requires careful size limiting per scope to prevent execution context from overflowing token limits.
