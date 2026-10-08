# ADR 0013 — Workload / Task Runnable Semantics

**Status:** Proposed (2026-10-08)
**Scope:** PersonaOS v0.1 extension (formalizes the gap between Step 7 and future scheduling)
**Supplements:** [ADR 0008 — Thin Worker, six tables](0008-thin-worker-six-tables.md), [ADR 0010 — WorkerExecutionKernel](0010-worker-execution-kernel.md), [ADR 0012 — Worker Orchestrator](0012-worker-orchestrator.md)

## Context

Steps 3-7 built the execution plumbing:

- **Step 3** — WorkerProfile versioning + immutable snapshots
- **Step 4** — WorkerExecutionContext (frozen bundle)
- **Step 5** — WorkerExecutionKernel (atomic claim via Phase 3)
- **Step 6** — Simulation/replay
- **Step 7** — WorkerOrchestrator (polling loop, kernel owns the claim)

The Step 7 orchestrator's `process_one()` currently does:

```sql
SELECT * FROM tasks
WHERE status = 'queued'
ORDER BY created_at ASC
LIMIT 1
```

This is a **find any queued task** query. It does NOT formalize what
"eligible to execute" means. The implicit assumption is:

> "If `status='queued'`, the task is runnable."

That assumption breaks the moment we add `dependencies`. The schema
already has the column (Step 1 migration):

```python
# src/personaos/db/models.py
dependencies: Mapped[list] = mapped_column(
    JSONB, nullable=False, server_default=text("'[]'::jsonb")
)
```

And `WorkloadService.enqueue()` accepts it. But nothing in the runtime
consults the field — it is dead code at the executor layer. A task with
`status='queued'` and an unsatisfied `dependencies` list will be picked
up by the orchestrator and sent to the kernel anyway. The kernel has
no way to know it shouldn't run yet.

Step 8 closes this gap by adding a deterministic runnable contract.

## Decision

### 1. Runnable predicate

A task is **runnable** iff:

```
task.status == 'queued'
AND
    task.dependencies == '[]'::jsonb
    OR (for every dep_id in task.dependencies, the task with that id has status='completed')
```

Equivalent SQL:

```sql
SELECT t.*
FROM tasks t
WHERE t.status = 'queued'
  AND (
        jsonb_array_length(t.dependencies) = 0
        OR NOT EXISTS (
            SELECT 1
            FROM jsonb_array_elements_text(t.dependencies) AS dep_id
            JOIN tasks dep ON dep.id = dep_id
            WHERE dep.status <> 'completed'
        )
  )
ORDER BY t.created_at ASC, t.id ASC
LIMIT 1
```

The first condition (`status='queued'`) is the lifecycle gate — a task
that has been claimed (`in_progress`), finished (`completed` /
`failed` / `canceled`), or held (`blocked` / `review`) is not runnable.

The second condition (dependencies) is the eligibility gate. The four
semantic outcomes, per Liang (2026-10-08):

| Dependencies | Runnable? | Reason |
|---|---|---|
| None | ✓ yes | No gate to satisfy |
| All `completed` | ✓ yes | Every dep has reached terminal success |
| Any `failed` | ✗ no | A dep will never recover → task stays `queued` (manual cleanup deferred) |
| Any `queued` / `in_progress` / `blocked` / `review` | ✗ no | A dep has not reached terminal yet |

### 2. Ordering — deterministic FIFO

```sql
ORDER BY t.created_at ASC, t.id ASC
```

- `created_at ASC` — primary FIFO ordering, mirrors Step 7.
- `id ASC` — deterministic tie-breaker for tasks created in the same
  microsecond (rare but possible). `id` is the task UUID string.

**Priority is explicitly OUT OF SCOPE for Step 8.** Step 8 does not
introduce a priority field, does not sort by `priority`, and does not
modify the `tasks.priority` column. The `tasks.priority` enum
(low / medium / high / critical) already exists in the schema (Step 1)
but is unused at the orchestrator layer — it stays unused. Adding
priority-aware selection is a future capability, deliberately
deferred.

### 3. Workload API — `next_runnable()`

Extend `WorkloadService` (do NOT introduce a new scheduler class) with:

```python
async def next_runnable(self) -> Task | None:
    """Return the next runnable candidate (FIFO), or None if no work.

    The returned task is NOT claimed. The caller (orchestrator) passes
    it to WorkerExecutionKernel.execute(), which performs the atomic
    claim (Phase 3: UPDATE WHERE status='queued').

    If two callers invoke concurrently, they may both receive the
    same task. The kernel's atomic claim ensures only one wins; the
    loser catches TaskStatusError — the same concurrency boundary
    established for Step 7.
    """
```

This is the **only** new API in Step 8. It extends `WorkloadService`,
which already owns task creation (`enqueue()`). No new module, no new
package, no new concept.

### 4. Orchestrator change — call `next_runnable()`

The orchestrator's `process_one()` replaces its raw SELECT with a call
to `WorkloadService.next_runnable()`:

**Before (Step 7):**

```python
task_row = (
    await self._session.execute(
        select(TaskModel)
        .where(TaskModel.status == "queued")
        .order_by(TaskModel.created_at)
        .limit(1)
    )
).scalar_one_or_none()
```

**After (Step 8):**

```python
workload = WorkloadService(self._session)
task = await workload.next_runnable()
```

That is the entire orchestrator change. Everything else stays:
- The kernel still owns the atomic claim.
- `TaskStatusError` is still the concurrency boundary.
- The orchestrator still doesn't claim.

### 5. No new state machine

Step 8 does **NOT** add:

```
blocked  / ready  / waiting  / scheduled
```

A task with unsatisfied dependencies stays `status='queued'`. The
runnable predicate filters it out at query time. There is no
on-disk signal of "waiting on deps" because:

- A task that becomes runnable when its deps complete does not need to
  transition status — it is already `queued`. The orchestrator's
  next query will pick it up.
- Avoids spurious transitions: a task with multiple deps might toggle
  between "1 dep pending" and "all deps done" repeatedly as siblings
  complete. A `ready` status would require its own event chain.
- Status should mean lifecycle state. Readiness is a query predicate,
  not a lifecycle state.

The failure mechanism (a dep reaches `'failed'`) is similar — the task
stays `queued`, and the predicate keeps excluding it. The
`failed_at`-style recovery (manually removing the dep, manually
canceling the task) is deferred to v0.2.

### 6. Static invariants (locked)

| # | Invariant | Enforcement |
|---|---|---|
| 1 | Existing `Task.dependencies` representation (JSONB list of task IDs) is the only dep model | No new column, no new table, no parallel dep structure |
| 2 | `next_runnable()` does NOT claim | Returns a domain Task; orchestrator passes to kernel which calls Phase 3 |
| 3 | The kernel's Phase 3 is still the only atomic claim path | Step 7 invariant preserved |
| 4 | FIFO ordering — no priority | Tests assert `created_at ASC, id ASC` |
| 5 | No new statuses (`blocked` / `ready` / `waiting` / `scheduled`) | AST check forbids new status literals |
| 7 | Runnable predicate handles missing deps (task with dep that doesn't exist) as "not runnable" | Test: `test_task_with_missing_dependency_is_not_runnable` (implicit via `test_task_with_pending_dependency_is_not_runnable`) |
| 8 | The orchestrator still doesn't import OpenClaw | Carried forward from Step 7 |

## What this ADR does NOT do

| Deferred | Reason |
|---|---|
| Priority scheduling | `tasks.priority` exists but unused; remains unused. Step 8 is FIFO only. |
| Dependency graph analysis | `dependencies` is a flat list of IDs. No topological sort, no cycle detection in v0.1 — enqueue()s are the user's responsibility. |
| Retry policy | Step 5 covers basic retries. Smarter policies are v0.2+. |
| Lease / failover / `execution_node` | Still deferred per ADR 0012 §"What's NOT in Step 7" |
| Distributed scheduling | v0.1 is single-process |
| LLM-based scheduling | Not applicable to a deterministic workload |
| Cycle detection on dependencies | A cycle means no task in the cycle is ever runnable. The orchestrator will not deadlock — it will simply find no runnable candidates. Surfacing cycles (cycles, pruning) is a future observability concern. |

For Step 8, the strongest first milestone is:

> **The orchestrator asks "what's next runnable?" instead of "what's next queued?". The runnable predicate formalizes the dependency gate. No new state machine, no new column, no new claim path.**

## Architecture

```
Workload
    │
    ▼
next_runnable()  ← Step 8 (this ADR)
    │
    ▼
Orchestrator.process_one()
    │
    ▼
WorkerExecutionKernel.execute()
    │
    ▼
atomic claim (Phase 3)  ← unchanged from Step 5
```

The `next_runnable()` call is a thin wrapper over the SQL pattern above.
It returns `None` when no task is runnable. The orchestrator treats
`None` exactly like Step 7 treated it (sleep, retry).

## Migration path

| Sub-step | Deliverable | Status |
|---|---|---|
| 8a | This ADR + spec + acceptance tests (this commit; tests skip until 8b) | **this commit** |
| 8b | `WorkloadService.next_runnable()` + orchestrator `process_one()` change + unskip tests | next |
| 8c | Tests pass; commit Step 8 as a single feature | bundled with 8b |

Target test count after Step 8: 62 (Step 7) + 8 (Step 8) = **70 passed, 0 skipped, 0 failed**.

## References

- [ADR 0008 — Thin Worker, six tables](0008-thin-worker-six-tables.md)
- [ADR 0010 — WorkerExecutionKernel](0010-worker-execution-kernel.md)
- [ADR 0012 — Worker Orchestrator](0012-worker-orchestrator.md)
- Spec: [`docs/specs/workload-runnable-semantics.md`](../specs/workload-runnable-semantics.md)
- Tests: `tests/integration/test_workload_runnable.py`