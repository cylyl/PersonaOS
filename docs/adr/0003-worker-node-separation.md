# ADR 0003 — Worker is identity, Node is execution location

**Status:** Accepted (2026-10-07)
**Scope:** PersonaOS v0.1

## Context

PersonaOS's thesis is that workers persist across model changes, engine restarts, and runtime changes. The README §5 lists five durable worker concerns: identity, skills/competence, experience/memory, workload/state, and performance.

If **Worker** and **Node** are conflated, persistence breaks: swapping the runtime (or losing the runtime) becomes indistinguishable from losing the worker.

## Decision

**Worker** and **Node** are separate domain entities from day one.

- **Worker** — persistent identity, profile, authority, history. Survives model + runtime changes.
- **Node** — execution location (host, capabilities, status, last heartbeat). Replaceable.

A worker moves: scheduler can reassign a worker from Node A to Node B without losing the worker's identity or experience.

```
worker devops-01
 │
 ├── Node A (was)
 │ ↓ failure
 │
 └── Node B (now)
       ↓
       resume task
```

The lease (`workload/lease.py`) binds a worker to a node for the duration of a single task. `node.lost` triggers lease expiry; scheduler reclaims and reassigns.

## Consequences

**Positive:**
- Even in single-node v0.1, the worker-row is paired with a node-row. Future multi-node is a non-event.
- Worker identity survives a runtime swap (model change, OpenClaw upgrade, GPU replacement).
- Capacity planning can reason about nodes (count, capabilities) separately from workers (count, skills).
- Heartbeats track node health without conflating with worker identity.

**Negative:**
- Two tables (`workers`, `nodes`) where some projects use one. Slightly more state.
- Worker-to-node assignment needs an explicit event (`worker.assigned`) so the audit trail is clear.

**Anti-patterns to avoid:**
- ❌ Storing `node_id` on the worker row — the worker outlives the node.
- ❌ Using `worker.last_heartbeat_at` — that's a node concern.
- ❌ Letting a worker carry "its node" as identity — the runtime is replaceable.
