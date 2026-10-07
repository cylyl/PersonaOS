# ADR 0005 — Runtime adapters are thin shims

**Status:** Accepted (2026-10-07)
**Scope:** PersonaOS v0.1

## Context

PersonaOS's thesis is "compute is replaceable; the worker is persistent." The worker should survive model changes, engine restarts, and changes to available tools (README §1).

If PersonaOS domain logic leaks into a runtime adapter (e.g., OpenClaw), the thesis breaks — the worker becomes coupled to one engine. Swapping OpenClaw for another runtime would mean rewriting domain logic.

## Decision

`execution/adapters/base.py` defines an `Adapter` Protocol with a single contract:

```python
class Adapter(Protocol):
    async def execute(self, task: Task, checkpoint: Checkpoint) -> Result: ...
```

That's it. No callbacks, no events, no shared state.

Concrete adapters (`openclaw.py`, future `local.py`, `cloud.py`) are thin shims:

1. Call the runtime API.
2. Translate the runtime's response back to PersonaOS's domain types.
3. Return.

Zero PersonaOS domain logic lives in an adapter. The runner, registry, scheduler, security, and policy modules do not import any concrete adapter — only the `Adapter` Protocol.

### Adapter placement

```
PersonaOS Worker (runner.py)
   │
   ▼
Adapter Protocol (base.py)
   │
   ├── OpenClaw adapter (openclaw.py)
   ├── Future local runtime (local.py)
   └── Future cloud runtime (cloud.py)
```

The worker remains portable across runtimes. Adapters are stubbed in tests via `Adapter` Protocol mocks.

## Consequences

**Positive:**
- Swapping OpenClaw for another runtime = swap the adapter implementation, no other code changes.
- Adapter failures are isolated — a buggy runtime can't corrupt PersonaOS state.
- Tests can use a `FakeAdapter` that returns canned responses, exercising the full runner loop without spinning up a real runtime.
- Future runtimes plug in without restructuring.

**Negative:**
- Adapters must translate domain types both ways — slight overhead.
- Runtime-specific features (e.g., OpenClaw's streaming) need to be expressed within the `Result` shape, which can lose fidelity.
- Two implementations = two test matrices to maintain.

**Anti-patterns to avoid:**
- ❌ Domain logic in `openclaw.py` (e.g., "if the task type is X, do Y" — that's runner logic).
- ❌ Adapters importing from `security/`, `workload/`, `registry/` — adapters are at the leaf of the dependency graph.
- ❌ Adapter-specific fields on the `Task` schema — the schema is runtime-agnostic.
- ❌ Multiple `Adapter` Protocols (one per runtime) — one Protocol, many implementations.
