# Task Dependency Lifecycle — Specification

**Status:** Draft v0.1 (2026-10-08)
**Companion to:** [ADR 0014 — Task Dependency Lifecycle](../adr/0014-task-dependency-lifecycle.md)

## 1. Concept

Step 8 formalized "Is this task runnable?" via `WorkloadService.next_runnable()`. Step 9 defines what happens at the boundaries of that predicate:

- When a dependency reaches `'failed'` — the dependent stays queued
- When dependencies form cycles (`A → B → A`) — neither is runnable
- When a task depends on itself (`A → A`) — it's not runnable
- When a dependency ID doesn't exist — the dependent is not runnable
- When multiple workers read the runnable set concurrently — answers are consistent

Step 9 introduces **no new states, no new module, no new method**. It formalizes query-level safety contracts that the existing Step 8 implementation already satisfies. The 10 acceptance tests verify these contracts against the current query.

```text
next_runnable()
    ↓
    SELECT * FROM tasks
    WHERE status='queued'
      AND (
            jsonb_array_length(dependencies) = 0
            OR NOT EXISTS (
                  SELECT 1
                  FROM jsonb_array_elements_text(dependencies) AS dep_id
                  LEFT JOIN tasks dep ON dep.id = dep_id
                  WHERE dep.id IS NULL              -- missing dep
                     OR dep.status <> 'completed'    -- dep not done
            )
          )
    ORDER BY created_at ASC, id ASC
    LIMIT 1
```

The query is **flat** (one JSONB expansion + JOIN), **read-only** (no mutation), and **bounded** (`LIMIT 1`). Step 9 verifies that this query is safe under pathological dep graph conditions.

## 2. Failure propagation

### Rule

```
Dependency failed
    ↓
Dependent remains queued (status='queued')
    ↓
next_runnable() never selects it
```

### What this means

- A dependent task with a failed dep is **permanently not runnable** in v0.1
- The dependent's status does **NOT** transition to `'failed'`, `'blocked'`, or any new status
- The dependent remains `status='queued'` until manual cleanup
- `next_runnable()` does NOT mutate any task state during this decision

### Why the query handles this correctly

The query excludes any candidate where `dep.status <> 'completed'`. Since `'failed' ≠ 'completed'`, a failed dep blocks its dependents. The dependent's status stays `'queued'` because the query only does `SELECT`, never `UPDATE`.

### Why no automatic cascade

Per ADR 0014 §Decision 1, failure cascades would require either new states or automatic status updates. Both add complexity without v0.1 use cases. "Stay queued forever" is the simplest, most predictable rule. Manual cleanup (cancel task, remove dep entry) is deferred to v0.2.

## 3. Cycles

### Rule

Cycles (`A → B → A`) and self-loops (`A → A`) are **not runnable**, with **no state mutation** and **no recursion or hang**.

### Why the query handles cycles correctly

The query is flat:
- For each candidate task, expand its `dependencies` JSONB array into one row per `dep_id`
- `LEFT JOIN` each `dep_id` against the `tasks` table
- Exclude candidates where any `dep_id` matches a task with `status <> 'completed'`

For cycles:

- **`A → B → A`**: For task A, the JSONB expansion yields `[B]`. B's status is `'queued'` (not `'completed'`). A is excluded. Same reasoning for B. Neither is runnable until both reach `'completed'` — which they can't, because they depend on each other. **Cycle tasks are permanently not runnable.**

- **`A → A`**: For task A, the JSONB expansion yields `[A]`. The `LEFT JOIN` matches `A.id = A.id`. A's status is `'queued'`. A is excluded. **Self-loop tasks are permanently not runnable.**

### Why this is query-level safety, not graph management

There is no:
- Graph walker
- Cycle-detection algorithm
- Recursive CTE
- Topological sort

The cycle handling is an emergent property of the `WHERE dep.status <> 'completed'` filter. Tasks in cycles keep each other out of the runnable set forever (which is the desired behavior).

## 4. Missing dependencies

### Rule

```
A → <nonexistent task>
```

→ A is not runnable. No state mutation.

### Why the query handles this correctly

The Step 8 query uses `LEFT JOIN`:

```sql
LEFT JOIN tasks dep ON dep.id = dep_id
WHERE dep.id IS NULL              -- missing dep
   OR dep.status <> 'completed'
```

`dep.id IS NULL` catches the missing-dep case (the `LEFT JOIN` finds no matching row in `tasks`). The dependent is excluded from the runnable set. No state mutation — the query only reads.

## 5. Concurrent completion

### Rule

Multiple concurrent calls to `next_runnable()` produce **consistent answers**. No reader sees a torn or inconsistent state.

### Why

- The query is read-only: no `UPDATE`, `INSERT`, or `DELETE`
- Postgres `READ COMMITTED` isolation: each statement sees the latest committed data
- Multiple concurrent readers see the same eligible tasks
- The atomic claim (kernel's Phase 3) is the only state mutation, and it's serialized by Postgres

### What the test must prove

Not just that two reads return the same answer. It must prove:

- Multiple concurrent readers + one writer (task completion) → consistent state
- No reader sees a torn state (e.g., a dep that was `'in_progress'` at read-start but `'completed'` at read-end)
- No deadlock or starvation

## 6. Acceptance criteria (10 tests)

| # | Criterion | Test |
|---|---|---|
| 1 | Completed dep makes task runnable | `test_completed_dependency_makes_task_runnable` |
| 2 | Failed dep keeps task non-runnable | `test_failed_dependency_keeps_task_non_runnable` |
| 3 | Pending dep keeps task non-runnable | `test_pending_dependency_keeps_task_non_runnable` |
| 4 | Missing dep keeps task non-runnable | `test_missing_dependency_keeps_task_non_runnable` |
| 5 | Self-loop dep keeps task non-runnable | `test_self_dependency_is_not_runnable` |
| 6 | Cycle (A→B→A) keeps both tasks non-runnable | `test_cyclic_dependencies_are_not_runnable` |
| 7 | Chain progresses step-by-step | `test_dependency_chain_progresses_after_each_completion` |
| 8 | Failed dep does NOT propagate `'failed'` status | `test_failed_dependency_does_not_propagate_failed_status` |
| 9 | Concurrent completion + reads → consistent state | `test_concurrent_dependency_completion_preserves_consistent_state` |
| 10 | `next_runnable()` is read-only for the dep graph | `test_next_runnable_remains_read_only_for_dependency_graph` |

## 7. What this spec does NOT define

Per ADR 0014:

- Cycle detection subsystem (cycle handling is query-side)
- Topology sort
- Auto-cancel on dep failure
- Dep failure notification
- Priority, retries, leases, failover, `execution_node`
- Distributed scheduling

All deferred per ADR 0012/0013/0014.

## 8. Implementation order

| Sub-step | Deliverable | Status |
|---|---|---|
| 9a | This spec + ADR 0014 + 10 acceptance tests (this commit; tests skip until 9b) | **this commit** |
| 9b | Tests unskip and pass against existing `next_runnable()` (no production diff) | next |
| 9c | Commit as documentation-only (no code change) | bundled with 9b |

Target test count: 70 (Step 8) + 10 (Step 9) = **80 passed, 0 skipped, 0 failed**.