# Workload / Task Runnable Semantics — Specification

**Status:** Draft v0.1 (2026-10-08)
**Companion to:** [ADR 0013 — Workload / Task Runnable Semantics](../adr/0013-workload-runnable-semantics.md)

## 1. Concept

Step 7's orchestrator finds tasks via `WHERE status='queued'`. This
specification adds a layer of **runnable semantics**: a task is
runnable iff its lifecycle gate is satisfied (`status='queued'`) AND
its dependency gate is satisfied (every declared dep has reached
`completed`).

The orchestrator's `process_one()` replaces its raw `SELECT` with a
call to `WorkloadService.next_runnable()`. Everything else stays:
the kernel still owns the atomic claim; `TaskStatusError` is still
the concurrency boundary.

```text
WorkloadService.next_runnable()
        ↓
Task | None
        ↓
orchestrator.process_one() — kernel.execute(task)
        ↓
WorkerExecutionKernel — atomic claim (Phase 3)
        ↓
Result → DB on sim task
```

## 2. Runnable predicate

A task is runnable iff:

```
task.status == 'queued'
AND
    task.dependencies is empty
    OR for every dep_id in task.dependencies, the task with that id
       has status='completed'
```

### 2.1 SQL pattern

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

The outer `WHERE` is the **lifecycle gate** (queued only). The inner
`AND` is the **dependency gate** (every dep must have reached
`completed`). The `ORDER BY` is deterministic FIFO with `id` as a
tie-breaker.

### 2.2 Dependency outcomes

| Dep state | Runnable? | Why |
|---|---|---|
| No deps (empty list) | ✓ yes | No gate to satisfy |
| All deps `completed` | ✓ yes | Terminal success reached |
| Any dep `failed` | ✗ no | A failed dep will never recover (v0.1) |
| Any dep `queued` / `in_progress` / `blocked` / `review` | ✗ no | Dep has not reached terminal yet |
| Any dep ID refers to a non-existent task | ✗ no | Degraded; treat as "not runnable" |

The "failed dep → permanently not runnable" behavior means the task
stays `queued` forever in v0.1. Manual cleanup (cancel, or remove
the dep entry) is deferred to v0.2.

## 3. Ordering — deterministic FIFO

```sql
ORDER BY t.created_at ASC, t.id ASC
```

- `created_at ASC` — primary FIFO ordering
- `id ASC` — deterministic tie-breaker for tasks created in the same
  microsecond (rare but possible). `id` is the task UUID string.

**Priority is OUT OF SCOPE for Step 8.** `tasks.priority` already
exists (low / medium / high / critical) but the orchestrator never
reads or sorts by it. Adding priority-aware selection is a future
capability, deliberately deferred.

## 4. API

### 4.1 `WorkloadService.next_runnable()`

```python
class WorkloadService:
    # ... existing enqueue(), etc. ...

    async def next_runnable(self) -> Task | None:
        """Return the next runnable candidate (FIFO), or None.

        The returned task is NOT claimed. The caller (orchestrator)
        passes it to WorkerExecutionKernel.execute(), which performs
        the atomic claim (Phase 3: UPDATE WHERE status='queued').

        If two callers invoke concurrently, they may both receive the
        same task. The kernel's atomic claim ensures only one wins;
        the loser catches TaskStatusError — the same concurrency
        boundary established for Step 7.

        Returns None when no task in the queue is runnable (empty
        queue, or all queued tasks have unsatisfied dependencies).
        """
```

**No new module.** Extends the existing `WorkloadService`. The
implementation depends on the same `AsyncSession` it already owns
(via `__init__`).

### 4.2 Orchestrator change

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
- `poll_interval` and the run/stop semantics are unchanged.

## 5. The flow

```python
# Setup (caller's job — unchanged from Step 7)
reg = ProfileRegistry(session=session, profiles_dir=profiles_dir)
await reg.register_worker(...)
await reg.create_profile_version(...)
await reg.activate_profile_version(..., 2)

# Enqueue work — dependencies list is just task IDs (existing API)
wl = WorkloadService(session=session)
await wl.enqueue(worker_id="w1", title="A", objective="A", input={})
# (no deps)
await wl.enqueue(
    worker_id="w1",
    title="B",
    objective="B",
    input={},
    dependencies=[task_a.id],   # B depends on A
)

# Orchestrate — process_one() now calls next_runnable()
adapter = CapturingAdapter(result_to_return=Result(status="completed", output={"x": 1}))
kernel = WorkerExecutionKernel(session=session, adapter=adapter, resolve_context=reg.resolve_execution_context)
orchestrator = WorkerOrchestrator(session=session, kernel=kernel)

# First iteration: only A is runnable (B depends on A which is queued)
await orchestrator.process_one()
# → processes A; A.result is set

# Second iteration: A is now 'completed', so B becomes runnable
await orchestrator.process_one()
# → processes B

# Third iteration: nothing runnable → returns False
processed = await orchestrator.process_one()
assert processed is False
```

## 6. Acceptance criteria

| # | Criterion | Test |
|---|---|---|
| 1 | A task with no dependencies is runnable | `test_task_without_dependencies_is_runnable` |
| 2 | A task with all deps `completed` is runnable | `test_task_with_all_dependencies_completed_is_runnable` |
| 3 | A task with a dep in `in_progress` (pending) is NOT runnable | `test_task_with_pending_dependency_is_not_runnable` |
| 4 | A task with a dep `failed` is NOT runnable | `test_task_with_failed_dependency_is_not_runnable` |
| 5 | Runnable tasks are returned in FIFO order (`created_at ASC, id ASC`) | `test_runnable_tasks_are_fifo_ordered` |
| 6 | A dependency chain becomes runnable only after the upstream dep completes | `test_dependency_chain_becomes_runnable_after_completion` |
| 7 | The orchestrator only picks runnable tasks (a `queued` task with unsatisfied deps is skipped) | `test_orchestrator_processes_only_runnable_tasks` |
| 8 | `next_runnable()` returns a candidate without claiming it — the kernel still owns the claim | `test_runnable_selection_does_not_claim_task` |

## 7. What this spec does NOT define

Per ADR 0013 — explicitly out of scope for Step 8:

| Deferred | Reason |
|---|---|
| Priority scheduling | `tasks.priority` exists but unused; remains unused. Step 8 is FIFO only. |
| Dependency graph analysis | `dependencies` is a flat list of IDs. No topological sort, no cycle detection in v0.1. |
| Cycle detection | A cycle means no task in the cycle is ever runnable — the orchestrator will not deadlock, it will simply find no runnable candidates. Surfacing cycles is a future observability concern. |
| Retry policy | Step 5 covers basic retries. Smarter policies are v0.2+. |
| Lease / failover / `execution_node` | Still deferred per ADR 0012 §"What's NOT in Step 7" |
| Distributed scheduling | v0.1 is single-process |
| Recovery from `failed` deps | Manual cleanup deferred to v0.2 |

For Step 8, the strongest first milestone is:

> **The orchestrator asks "what's next runnable?" instead of "what's next queued?". The runnable predicate formalizes the dependency gate. No new state machine, no new column, no new claim path.**

## 8. Implementation order

| Sub-step | Deliverable |
|---|---|
| 8a | This spec + ADR 0013 + acceptance tests (this commit; tests skip until 8b) |
| 8b | `WorkloadService.next_runnable()` + orchestrator `process_one()` change + unskip tests |
| 8c | Tests pass; commit Step 8 as a single feature |

Target test count after Step 8: 62 (Step 7) + 8 (Step 8) = **70 passed, 0 skipped, 0 failed**.