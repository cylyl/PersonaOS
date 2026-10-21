# ADR 0014 — Task Dependency Lifecycle

**Status:** Proposed (2026-10-08)
**Scope:** PersonaOS v0.1 (formalizes dependency lifecycle semantics)
**Supplements:** [ADR 0008 — Thin Worker, six tables](0008-thin-worker-six-tables.md), [ADR 0013 — Workload Runnable Semantics](0013-workload-runnable-semantics.md)

## Context

Step 8 formalized "Is this task runnable?" via `WorkloadService.next_runnable()`. The query checks:

- `task.status == 'queued'` (lifecycle gate)
- All deps have `status='completed'` (dependency gate)

Step 8 didn't fully address what happens at the boundaries of that predicate:

- **Failure propagation**: a dependency reaches `'failed'` — what happens to dependents?
- **Cycles**: `A → B → A` — do these ever become runnable?
- **Self-dependencies**: `A → A` — same question.
- **Missing dependencies**: `A → <nonexistent task>` — silent skip or explicit failure?
- **Concurrent completion**: multiple workers reading the runnable set while a kernel is committing a task completion — torn or consistent state?

Step 9 defines these as **query-level safety contracts**, not graph management. We do NOT introduce a cycle-detection subsystem, a topology sorter, or new task states. The existing `next_runnable()` query (Step 8) is flat, read-only, and bounded — Step 9 verifies it is safe under pathological dep graph conditions.

## Decision

### 1. Failure propagation — explicit rule

```
Dependency failed
    ↓
Dependent remains queued (status='queued')
    ↓
next_runnable() never selects it
```

- A dependent task with a failed dep is **permanently not runnable** in v0.1
- The dependent's status does **NOT** transition to `'failed'`, `'blocked'`, or any new status
- The dependent remains `status='queued'` until manual cleanup
- Manual cleanup paths (v0.2+, deferred): `UPDATE tasks SET status='canceled'` or `UPDATE tasks SET dependencies='[]'::jsonb WHERE id=?`

**Why no automatic cascade:** failure cascades would require either (a) new states (`'failed_due_to_dependency'`) or (b) automatic status updates when a dep fails. Both add complexity without v0.1 use cases. "Stay queued forever" is the simplest, most predictable rule.

### 2. Cycles — query-level safety, not graph management

For v0.1:

```
A → B → A   (cycle)
A → A      (self-loop)
```

Both must be:

- **Not runnable** — `next_runnable()` excludes them
- **No state mutation** — status remains `'queued'`, no special flag set
- **No recursion or hang** — the query is flat (one JSONB expansion + JOIN), bounded by `LIMIT 1`
- **No graph-management subsystem** — no `find_cycles()` method, no cycle-detection algorithm, no topological sort

This is a **query-level safety contract**. Cycles are handled as an emergent property of the `WHERE dep.status <> 'completed'` filter:

- `A → B → A`: For task A, the JSONB expansion yields `[B]`. The LEFT JOIN finds B with `status='queued'` (not `'completed'`). The `NOT EXISTS` clause excludes A. Same reasoning for B. Neither is runnable until both reach `'completed'` — which they can't, because they depend on each other. **Cycle tasks are permanently not runnable** (correct behavior).

- `A → A`: For task A, the JSONB expansion yields `[A]`. The LEFT JOIN matches `A.id = A.id`. A's status is `'queued'` (not `'completed'`). The `NOT EXISTS` clause excludes A. **Self-loop tasks are permanently not runnable** (correct behavior).

### 3. Missing dependencies — handled by existing `LEFT JOIN`

```
A → <nonexistent task>
```

→ A is not runnable. No state mutation.

The Step 8 query already handles this:

```sql
LEFT JOIN tasks dep ON dep.id = dep_id
WHERE dep.id IS NULL              -- missing dep
   OR dep.status <> 'completed'    -- dep not done
```

`dep.id IS NULL` catches the missing-dep case (the LEFT JOIN finds no matching row in `tasks`). The dependent is excluded from the runnable set.

### 4. Concurrent completion — read-only query guarantees consistency

`next_runnable()` is a **read-only SELECT**. Concurrent calls produce consistent answers because:

- No `UPDATE`, `INSERT`, or `DELETE` in the query
- Postgres `READ COMMITTED` isolation: each statement sees the latest committed data at statement start
- Multiple concurrent readers see the same eligible tasks
- The atomic claim (kernel's Phase 3) is the only state mutation, and it's serialized by Postgres

The test must prove: **concurrent reads + concurrent dep completion → consistent state**. Not just that two reads return the same answer, but that a writer (task completion) and readers can interleave without producing torn states.

### 5. No new state machine

Step 9 does **NOT** add:

- `'blocked'` (or any synonym: `'waiting'`, `'ready'`, `'scheduled'`)
- `'failed_due_to_dependency'`
- Any new column on `tasks`
- Any new table

Task stays `status='queued'` while deps are unsatisfied. Runnability is a query predicate, not a lifecycle state. This is the same invariant as Step 8 (ADR 0013 §Decision 5).

### 6. Static invariants

| # | Invariant | Enforcement |
|---|---|---|
| 1 | `next_runnable()` remains read-only | No UPDATE/INSERT/DELETE in the query |
| 2 | Failure does not cascade to dependent's status | `test_failed_dependency_does_not_propagate_failed_status` |
| 3 | Cycle tasks are not runnable, no mutation | `test_cyclic_dependencies_are_not_runnable` |
| 4 | Self-loop tasks are not runnable, no mutation | `test_self_dependency_is_not_runnable` |
| 5 | Missing dep → not runnable, no mutation | `test_missing_dependency_keeps_task_non_runnable` |
| 6 | Failed dep → dependent remains queued | `test_failed_dependency_keeps_task_non_runnable` |
| 7 | Pending dep → not runnable | `test_pending_dependency_keeps_task_non_runnable` |
| 8 | Concurrent reads + writes → consistent state | `test_concurrent_dependency_completion_preserves_consistent_state` |
| 9 | `next_runnable()` does not mutate dep graph | `test_next_runnable_remains_read_only_for_dependency_graph` |
| 10 | Kernel is the lifecycle owner | No status transitions in WorkloadService |
| 11 | No priority, retry, lease, failover, execution_node | No new code introduced |

## What this ADR does NOT do

| Deferred | Reason |
|---|---|
| Cycle detection subsystem | Cycle handling is query-side, not graph-management |
| Topology sort | Not needed for v0.1 FIFO ordering |
| Auto-cancel on dep failure | Manual cleanup deferred to v0.2 |
| Dep failure notification | Deferred (no events layer yet) |
| Priority | Deferred per ADR 0012/0013 |
| Retries | Deferred |
| Leases | Deferred |
| Failover / `execution_node` | Deferred |
| Distributed scheduling | v0.1 is single-process |

For Step 9, the strongest first milestone is:

> **The existing `next_runnable()` query is verified safe under failure propagation, cycles, self-loops, missing deps, and concurrent reads. No new states, no new code, no new column.**

## Architecture (unchanged from Step 8)

```
Workload
    │
    ▼
next_runnable()  ← Step 9 verifies safety contracts (read-only)
    │
    ▼
Orchestrator.process_one()
    │
    ▼
WorkerExecutionKernel.execute()
    │
    │ atomic claim (Phase 3)
    ▼
execution
```

## Migration path

| Sub-step | Deliverable | Status |
|---|---|---|
| 9a | This ADR + spec + 10 acceptance tests (this commit; tests skip until 9b) | **this commit** |
| 9b | Tests unskip and pass against existing `next_runnable()` (no production diff) | next |
| 9c | Commit as documentation-only (no code change) | bundled with 9b |

Target test count after Step 9: 70 (Step 8) + 10 (Step 9) = **80 passed, 0 skipped, 0 failed**.

## Acceptance tests (10 tests)

1. `test_completed_dependency_makes_task_runnable`
2. `test_failed_dependency_keeps_task_non_runnable`
3. `test_pending_dependency_keeps_task_non_runnable`
4. `test_missing_dependency_keeps_task_non_runnable`
5. `test_self_dependency_is_not_runnable`
6. `test_cyclic_dependencies_are_not_runnable`
7. `test_dependency_chain_progresses_after_each_completion`
8. `test_failed_dependency_does_not_propagate_failed_status`
9. `test_concurrent_dependency_completion_preserves_consistent_state`
10. `test_next_runnable_remains_read_only_for_dependency_graph`

## References

- [ADR 0008 — Thin Worker, six tables](0008-thin-worker-six-tables.md)
- [ADR 0013 — Workload Runnable Semantics](0013-workload-runnable-semantics.md)
- Spec: [`docs/specs/task-dependency-lifecycle.md`](../specs/task-dependency-lifecycle.md)
- Tests: `tests/integration/test_task_dependency_lifecycle.py`