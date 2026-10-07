# ADR 0008 Addendum — Versioned WorkerProfile + Immutable Task Snapshots

**Status:** Proposed (2026-10-07)
**Scope:** PersonaOS v0.1 extension (no new tables)
**Supplements:** [ADR 0008 — Thin Worker model, six tables for v0.1](0008-thin-worker-six-tables.md)

## Context

ADR 0008 establishes the v0.1 schema: six tables, thin Worker, no v0.2 features. But it leaves an open question: **how does a worker's configuration evolve over time without mutating its historical behavior?**

A worker that changes personality (e.g., becomes more cautious after an incident) must NOT erase its past behavior. Tasks that ran under the old persona must remain reproducible forever: "this task ran with persona v2" must be answerable even after v3 is activated.

This addendum adds a versioning layer for worker configuration **without adding any new tables** — strictly within the six-table boundary.

## Decision

### 1. Two column additions on existing tables

```sql
-- workers: pointer to the active profile version
ALTER TABLE workers ADD COLUMN current_profile_version INTEGER NOT NULL DEFAULT 1;

-- tasks: snapshot of the profile version active when this task was enqueued
ALTER TABLE tasks ADD COLUMN profile_version INTEGER NOT NULL DEFAULT 1;
```

No new tables. The `worker_profile_versions` table is **explicitly deferred to v0.2+** — for v0.1, profile versions live as **immutable YAML files on disk**.

### 2. Profiles live as immutable files

```
profiles/
├── worker-devops-001/
│   ├── v1.yaml        ← immutable forever
│   ├── v2.yaml        ← immutable forever
│   └── v3.yaml        ← current (workers.current_profile_version = 3)
├── worker-developer-001/
│   └── v1.yaml
└── worker-qa-001/
    └── v1.yaml
```

- File path encodes `worker_id` + `version`
- Files are written **once** and never modified
- To "change" v2, you create v3. v2 stays as-is forever
- File system acts as the version log
- Backed up with the same care as the database (it IS the version log)

### 3. WorkerProfile ≠ Persona (separation of concerns)

```
WorkerProfile (versioned container)
├── Identity        (role, description)
├── Persona         (behavioral identity — style, behavior, goals, constraints)
├── Model           (LLM model reference — placeholder in v0.1)
├── Instructions    (system prompt)
├── Skills          (snapshot of worker_skills)
├── Permissions     (snapshot of workers.permissions)
└── Runtime config  (runtime adapter, max_concurrent_tasks)
```

**Persona** is the *behavioral* subset (style, behavior, goals, constraints).  
**WorkerProfile** is the *versioned assignment* of Persona + operational config to a Worker.

Why this matters: personality ≠ operational profile. A worker's persona (cautious, analytical) is conceptually stable across most operational changes — a worker might switch runtime adapter or adjust `max_concurrent_tasks` without those being "persona changes." Conflating them creates a giant configuration object that resists evolution.

### 4. Invariants (no exceptions)

| # | Invariant | Enforcement |
|---|---|---|
| 1 | A worker has **one active profile version** | `workers.current_profile_version` is a single integer; activation sets it atomically |
| 2 | A profile version is **immutable after activation** | Files written once, no in-place edits; registry rejects version-overwrite |
| 3 | Every task captures the exact **persona/profile version** used | `tasks.profile_version` set at enqueue time, never updated |
| 4 | Historical tasks remain reproducible | `tasks.profile_version` + the immutable file gives you the exact persona used |
| 5 | Activation is **atomic** | Single transaction: validate file → update workers table → bump pointer; all-or-nothing |
| 6 | The registry owns profile lifecycle | No direct file edits; all writes via `registry/profile.py` |

### 5. Activation semantics

`POST /workers/{id}/persona/{v}/activate`:

1. Validate `profiles/{id}/v{v}.yaml` exists and parses
2. Load profile fields
3. **Atomic DB update** (single transaction):
   ```sql
   UPDATE workers
   SET role = :role,
       personality = :personality,
       capabilities = :capabilities,
       permissions = :permissions,
       runtime = :runtime,
       max_concurrent_tasks = :max_concurrent_tasks,
       current_profile_version = :v
   WHERE id = :id
   ```
4. New tasks enqueued from this point forward get `tasks.profile_version = v`
5. **The file at `profiles/{id}/v{v}.yaml` is never touched**

### 6. Task snapshot

When a task is enqueued:

```sql
INSERT INTO tasks (..., profile_version, ...)
VALUES (..., (SELECT current_profile_version FROM workers WHERE id = :worker_id), ...);
```

`tasks.profile_version` is captured at enqueue and **never updated**. This is the snapshot.

To reproduce a task's behavior:
1. Load `tasks.profile_version` (e.g., `2`)
2. Load `profiles/{worker_id}/v2.yaml` (immutable, forever)
3. Run the task with that exact persona + config

## Consequences

**Positive:**

- Worker identity is stable; configuration evolves via new versions
- Old tasks remain reproducible forever (file-based history)
- A/B testing: assign half the workers v2, half v3, measure task outcomes
- Rollback: `activate v1` reverts operational state
- Audit: every task knows its persona version
- No new tables — six-table boundary preserved

**Negative:**

- Profile files must be backed up with the same care as the database
- Workers table fields are duplicated in the profile file — single source of truth becomes "either DB or file depending on the question"
- File-system permissions must prevent accidental edits (mitigated by registry-only writes)
- YAML schema validation is manual for v0.1 (pydantic deferred to v0.2)

## What stays deferred (per ADR 0008 spirit)

- `worker_profile_versions` DB table — defer until SQL-level versioning is needed (audit reports, time-travel queries)
- `tasks.execution_node`, `tasks.attempt`, `tasks.lease_expires_at` — defer until the **failover acceptance test** drives the shape (per ADR 0008 "Future evolution" section)
- Profile diff UI
- Automatic promotion / canary rollouts
- Profile inheritance / composition
- LLM model selection (`model` field is a placeholder in v0.1)
- pydantic-based YAML schema validation (manual review for v0.1)
- Profile search / discovery (registry knows by worker_id only)

## Future evolution (driven by testing)

The **killer test** that drives the next evolution:

> "Stop Node A → start Node B → PersonaOS resumes the same task without losing the worker's identity or task state."

When that test is implemented and surfaces the need for:

- `tasks.execution_node` — current runtime location (vs the historical `worker_nodes` binding)
- `tasks.attempt` — retry counter
- `tasks.lease_expires_at` — when the scheduler should reclaim

…those columns get added in a v0.2 migration. **Until then, the two columns added in this addendum are the only schema changes** beyond the original six tables.

**This matches the ADR 0008 "Future evolution: `assigned_worker` is not a lease" caveat** — design emerges from testing, not from speculation.

## References

- [ADR 0008 — Thin Worker model, six tables for v0.1](0008-thin-worker-six-tables.md)
- Spec doc: [`docs/specs/worker-profile.md`](../specs/worker-profile.md)
