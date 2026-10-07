# ADR 0006 — Events are first-class data, not infrastructure

**Status:** Accepted (2026-10-07)
**Scope:** PersonaOS v0.1

## Context

The runner's hot path benefits from a complete event log: replay, audit, simulation, and recovery all depend on it. But full event sourcing (rebuilding every state from events) adds complexity that v0.1 doesn't need:

- Projections must be maintained for every read model.
- Replay tooling needs to handle schema evolution across event versions.
- Async event handlers become a second source of truth.

For v0.1, state tables are simpler and faster. But if we treat events as second-class, the migration to event sourcing later becomes painful.

## Decision

Events are a separate `events` table in Postgres (append-only) with a typed `type` column constrained to the 17 v0.1 event types:

```
worker.created, worker.updated, worker.assigned, worker.started, worker.stopped,
task.created, task.claimed, task.checkpointed, task.completed, task.failed,
node.joined, node.heartbeat, node.lost,
policy.allowed, policy.denied,
worker.quarantined, approval.requested
```

State tables (`workers`, `nodes`, `tasks`, `checkpoints`, `policies`) **remain the source of truth**. The event log is appended to as a **side effect** of state changes — a single transaction writes the state change AND appends the event.

```
events (
    id          UUID PRIMARY KEY,
    type        TEXT NOT NULL CHECK (type IN (...17 values...)),
    actor_id    TEXT NOT NULL,
    task_id     TEXT,
    payload     JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
```

### v0.1 surface

`events` exposes only:

- `events.append(type, actor_id, task_id=None, payload=None)` — synchronous, inside the same transaction as the state change.
- `events.list(type=None, since=None, limit=100)` — read-only API for `/events` route.

No projection engine, no replay tool, no async handlers in v0.1. The **foundation** is laid; the complex machinery waits.

## Consequences

**Positive:**
- Future migration to full event sourcing doesn't require touching state tables — events already exist and are append-only.
- Audit trails are automatic — every state change has a corresponding event.
- Replay/recovery tooling can be added later without breaking the storage model.
- v0.1 ships in 4–6 months because we don't build the projection layer.

**Negative:**
- Events are a write-amplification cost (one extra row per state change).
- Two queries are needed for some read patterns (state + history). The `/events` route covers history reads.
- The 17-type enum must be updated whenever a new event type is introduced — migration is explicit.

**Anti-patterns to avoid:**
- ❌ Reading events and reconstructing state in v0.1 — that's event sourcing, deferred.
- ❌ Async event handlers that write to other tables — events are a log, not a workflow engine.
- ❌ Treating events as "audit-only" — they're the foundation for replay, simulation, and recovery too.
- ❌ Schema-less event payloads (`payload JSONB` with no validation) — use the `type` enum to constrain what's allowed, validate `payload` against a per-type schema when generated from `schemas/event.yaml`.
