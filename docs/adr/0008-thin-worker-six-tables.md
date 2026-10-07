# ADR 0008 — Thin Worker model, six tables for v0.1

**Status:** Accepted (2026-10-07)
**Scope:** PersonaOS v0.1

## Context

Initial v0.1 design (committed at 99dcf11) embedded worker profile, authority, skills, and permissions inside the `workers` row. That made Worker a *fat* record conflating identity with behavior.

Fat workers don't scale:

- Every profile change rewrites the worker row.
- Every skill addition rewrites the worker row.
- Every permissions change rewrites the worker row.
- Memory, history, events, conversations all want to live on the worker — making it a write bottleneck and a privacy hazard.
- A worker can never be deleted without losing its history; a worker can never move domains without rewriting its body.

The README §5 lists five durable concerns: identity, skills, experience, workload, performance. Conflating them in one row violates the very separation the README proposes.

## Decision

### 1. Worker is a thin identity record

Carries only what defines *who* the worker is:

| Field | Purpose |
|---|---|
| `id`, `name`, `role`, `description` | Identity |
| `status` | `idle` \| `busy` \| `paused` \| `quarantined` \| `retired` |
| `runtime` | Default adapter (`openclaw` in v0.1) |
| `max_concurrent_tasks` | Capacity limit |
| `personality` | JSONB `{style, autonomy}` — LLM-independent |
| `capabilities`, `permissions` | JSONB lists (no separate join table in v0.1) |
| `memory_id` | Pointer to Memory store; **null in v0.1** |
| `created_at`, `updated_at`, `last_seen_at` | Lifecycle |

Everything else — **skills, task history, events, conversation state, memory contents, tool logs** — lives in separate stores and is referenced from Worker by ID, not embedded.

### 2. Six core tables for v0.1

| Table | Purpose |
|---|---|
| `workers` | Thin identity (above) |
| `tasks` | Unit of work (id, title, objective, status, priority, required_skills, required_capabilities, assigned_worker, assigned_node, dependencies, input, checkpoint ref, result, timestamps) |
| `worker_skills` | `(worker_id, skill)` join table; the scheduler queries this for matching |
| `worker_nodes` | Current/historical binding (worker ↔ node); replaces the `leases` table |
| `task_events` | Events related to tasks; CHECK-constrained to the 5 task event types emitted in v0.1 (`task.created`, `task.claimed`, `task.checkpointed`, `task.completed`, `task.failed`) |
| `task_checkpoints` | Durable state snapshots for resumable tasks |

### 3. The Phase-1 scheduler flow

```
Task requirements
   ↓
Skill match       → worker_skills
   ↓
Permission match  → workers.permissions (JSONB)
   ↓
Availability      → workers.status + max_concurrent_tasks
   ↓
Node capability   → worker_nodes.current_node_id
   ↓
Worker workload   → COUNT(tasks WHERE assigned_worker = ? AND status IN ('assigned','in_progress'))
   ↓
Assign
```

### 4. Deferred to v0.2

| Deferred | Reason |
|---|---|
| `policies` table | v0.1 uses inline rules in the scheduler; v0.2 if explicit policy management is needed |
| `leases` table | Replaced by `worker_nodes` binding + `tasks.assigned_worker` |
| `pending_approvals` table | Defer human approval gates to v0.2 |
| Worker / node events (`worker.assigned`, `node.joined`, `node.heartbeat`, `node.lost`, `worker.quarantined`, `approval.requested`) | Not emitted in v0.1; v0.2 if needed for ops dashboards |
| Unified `events` table | v0.1 ships `task_events` only; v0.2 if cross-domain event queries are needed |
| pgvector | Phase 2 with Memory (per ADR 0007) |

## Consequences

**Positive:**

- Worker writes become rare — only identity changes touch the row.
- Skill additions, permission changes, history all hit their own tables; no write contention.
- Privacy boundary: a worker can be deleted from `workers` without losing its task history (FK cascades on `worker_skills` + `worker_nodes`; `tasks.assigned_worker` becomes NULL via ON DELETE SET NULL).
- The Phase-1 acceptance test (worker moves between nodes) is naturally supported by `worker_nodes` history.
- Six tables is the smallest viable surface for the kernel — no premature schema design.

**Negative:**

- Profile, memory, history all need their own stores (Memory adapter, Task history, Event store) — more moving parts than one fat table.
- `worker_skills` and `worker_nodes` need explicit schema migrations; small overhead.
- `workers.capabilities` and `workers.permissions` as JSONB lose some query-ability vs dedicated join tables; acceptable for v0.1.

**Anti-patterns to avoid:**

- ❌ Adding fields to `workers` because "it's just one more thing" — that's how the fat-worker trap re-emerges.
- ❌ Joining `task_events` with the unified `events` table — `task_events` is intentionally separate for v0.1's six-table scope.
- ❌ Re-introducing `leases` as a separate concept — `worker_nodes` binding + `task.assigned_worker` covers it.
- ❌ Adding `policies` table before we need explicit policy management.

## Migration path

- **v0.1** — ship the 6 tables.
- **v0.2** — add `policies`, `pending_approvals`, worker/node events, unified `events` table if the task-only subset proves limiting.
- **Phase 2** — add pgvector when Memory lands (per ADR 0007).

## On disk now (refinements per this ADR)

| File | Change |
|---|---|
| `schemas/worker.yaml` | Thin identity schema; capabilities + permissions as JSONB arrays; no embedded skills |
| `schemas/task.yaml` | `title` + `objective` + `input` (his model); `assigned_worker` / `assigned_node` naming |
| `src/personaos/db/models.py` | Six-table summary in `_v01_schema_summary()` docstring; tasks/worker_nodes/task_events/task_checkpoints documented |
| `examples/{devops,developer,qa}-worker.yaml` | Seed format — split by `make seed` into `workers` + `worker_skills` |
| `VIKUNJA.md` | Success criteria + the killer acceptance test added |

## Future evolution: `assigned_worker` is not a lease

For v0.1, `tasks.assigned_worker` + `worker_nodes` binding is sufficient. But it's *not* a true lease — `assigned_worker` doesn't track:

- Whether the worker is *actively executing* the task (vs the assignment being stale after a crash)
- How many *attempts* the worker has made on this task
- What *execution_node* the work is currently running on (vs the historical binding)
- When the lease expires (so the scheduler can reclaim stuck tasks)

When failover is implemented, the schema will need to evolve:

```sql
-- v0.2 sketch (DO NOT add to v0.1)
ALTER TABLE tasks ADD COLUMN execution_node text;          -- current runtime location
ALTER TABLE tasks ADD COLUMN attempt integer NOT NULL DEFAULT 1;
ALTER TABLE tasks ADD COLUMN lease_expires_at timestamptz;
-- or: separate leases table (task_id, worker_id, node_id, attempt, expires_at, renewed_at)
```

The Phase-1 acceptance test (stop node → start different node → load worker → load checkpoint → resume) will surface the exact requirement when implemented. **Don't pre-build the lease concept into the v0.1 schema** — let the test drive the shape. The schema is small *because* we haven't implemented failover yet; the moment failover works, this ADR gets superseded.

For now: respect `tasks.assigned_worker` as the *current assignment* and assume the worker is either actively executing or recently failed. The runner's heartbeat + checkpoint load + `task_checkpoints` write cycle is what proves a worker is alive in v0.1.

**Anti-pattern to avoid:** assuming `assigned_worker = currently executing` forever. It works in the happy path. It breaks the moment a worker dies mid-task.
