# ADR 0007 — Memory behind an Adapter Protocol, scoped by category

**Status:** Accepted (2026-10-07)
**Scope:** PersonaOS Phase 2 (Memory is deferred from v0.1)

## Context

PersonaOS workers accumulate experience, decisions, user preferences, project context, and task history. Without a memory layer, workers are amnesiac across sessions — the opposite of the README's thesis: *"Compute is replaceable; the worker is persistent."*

Per the README §6, memory has five categories: working, episodic, semantic, procedural, and worker profile. But the README §6 does not specify **how** memory is implemented — only what categories it must cover.

Three external memory engines are viable candidates:

| Engine | What it is | Why we considered it |
|---|---|---|
| **Mem0** | Apache 2.0, Python, designed as an agent memory layer with extraction, dedup, conflict resolution | Lowest-friction integration. Doesn't impose a runtime. |
| **Graphiti** | Temporal knowledge graph for agents; tracks how facts evolve over time | Solves the "facts change over time" problem once workers have months of history. |
| **Letta** | MemGPT-style agent-managed memory; agent curates its own working set | Conceptually beautiful, but comes with its own agent runtime. |

Building a memory engine from scratch is out of scope for Phase 2 and beyond.

## Decision

1. **Memory is behind an `Adapter` Protocol** — extending the runtime-adapter pattern from ADR 0005. One interface, many implementations. Swapping Mem0 → Graphiti is config, not rewrite.

2. **Memory is scoped, not global.** Five scopes:

   | Scope | Use |
   |---|---|
   | `persona` | Preferences, working style, long-term goals |
   | `project` | Per-project memory (PersonaOS, FMS, etc.) |
   | `worker` | Per-worker (CTO, Developer, QA, DevOps) |
   | `task` | Current / completed / failed |
   | `knowledge` | Architecture, decisions, documentation |

   Retrieval filters by scope and category **before** vector/graph retrieval. Example: CTO asks *"What did we decide about PersonaOS memory?"* → `scope=project:personaos, type=decision` — not a global search.

3. **Phase 2 ships Mem0 only.** Solves dedup/conflict, fast to integrate, doesn't impose a runtime. pgvector for vector storage (added back to `docker-compose.yml` at Phase 2). Redis for short-term / task state.

4. **Graphiti is added when temporal reasoning becomes a real need.** When workers accumulate months of history and "facts change over time" matters (user preference drift, worker skill evolution, project state changes), migrate behind the same Protocol. **No worker code changes.**

5. **Letta is not used.** Its agent-managed memory model conflicts with PersonaOS's own runner. Two runners arguing over who owns the worker is a deal-breaker.

## Adapter surface

```python
class MemoryAdapter(Protocol):
    """Per ADR 0005: thin shim around an external memory engine."""

    async def remember(
        self,
        scope: Scope,
        owner_id: str,
        category: Category,         # working | episodic | semantic | procedural | profile
        content: str,
        metadata: dict | None = None,
    ) -> MemoryRef: ...

    async def recall(
        self,
        scope: Scope,
        owner_id: str,
        query: str,
        category: Category | None = None,
        limit: int = 10,
        at_time: datetime | None = None,  # temporal; Mem0 ignores, Graphiti uses
    ) -> list[MemoryRef]: ...

    async def update(self, ref: MemoryRef, content: str, metadata: dict | None = None) -> MemoryRef: ...
    async def forget(self, ref: MemoryRef) -> None: ...
    async def list(self, scope: Scope, owner_id: str, category: Category | None = None) -> list[MemoryRef]: ...
```

Two concrete adapters planned:

- `src/personaos/memory/adapters/mem0.py` — Phase 2
- `src/personaos/memory/adapters/graphiti.py` — Phase 4 (when temporal reasoning matters)

Configured via `MEMORY_ADAPTER=mem0|graphiti` in `.env`.

## Consequences

**Positive:**
- Workers stop being amnesiac. The README's persistent-worker thesis becomes enforceable.
- Scope-based retrieval is fast and predictable — no global RAG over everything.
- Engine migration (Mem0 → Graphiti) doesn't touch worker code.
- `MemoryAdapter` Protocol reuses the ADR 0005 adapter discipline.

**Negative:**
- Dedup is LLM-assisted — every memory write may call an LLM to decide novelty. Budget for this.
- Mem0's PG schema doesn't match ours. A thin sync layer between Mem0 tables and our `events` log is needed (or accept memories are out-of-band of the canonical event flow). ADR-pending.
- pgvector returns to `docker-compose.yml` at Phase 2 (was deferred from v0.1).
- Per-worker scoping must be enforced at the API level, not just metadata — Mem0's `user_id` filter is the right primitive, but we wrap it.

**Anti-patterns to avoid:**

- ❌ Global memory search across all scopes — slow, low precision, no privacy boundary
- ❌ Putting memory writes in the runner hot path — they're a side effect, not a precondition
- ❌ Worker memory leaking across workers — every call MUST include a scope + owner_id
- ❌ Skipping the adapter layer and calling Mem0 directly from worker code — kills the Graphiti migration path

## Migration path

- **Phase 2** — `Mem0Adapter` ships behind the Protocol. pgvector added to `docker-compose.yml`. Worker runner can `await memory.remember(...)` and `await memory.recall(...)`.
- **Phase 4** — If temporal reasoning becomes a real need, add `GraphitiAdapter`. Workers continue calling `MemoryAdapter`; only the config changes.

## Why not just use Mem0 directly?

Two reasons:

1. **Graphiti will eventually win some queries.** Mem0's vector-first ranking doesn't capture "this fact used to be true" — the temporal axis. When that matters, the migration has to be a config change, not a worker rewrite.
2. **Per-worker / per-project scoping needs to be enforced at the API.** Mem0's `user_id` filter works, but a PersonaOS worker should never be able to call `memory.search()` without a scope. The Protocol enforces that; calling Mem0 directly doesn't.

The adapter layer is ~150 lines of glue. It buys engine-portability and per-worker isolation for cheap.
