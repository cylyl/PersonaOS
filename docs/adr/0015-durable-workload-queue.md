# ADR 0015 — Durable Workload Queue and Task Lifecycle

**Date:** 2026-10-09
**Status:** Draft
**Deciders:** Liang, OpenClaw

---

## Context

PersonaOS v0.1 already stores tasks in PostgreSQL and uses `WorkerExecutionKernel.execute()` for atomic task ownership. Steps 5–9 established the workload runnable semantics, execution context, simulation/replay, task dependency lifecycle, and kernel lifecycle ownership.

This ADR addresses: tasks must survive orchestrator and application process restarts, and the task lifecycle must have well-defined, enforceable state transitions.

---

## Decision

### 1. Queued-task durability (in scope for v0.1)

A task with `status = 'queued'` persists in PostgreSQL across process restarts. `next_runnable()` reads directly from the database — there is no in-memory queue. An orchestrator that restarts will rediscover queued tasks via `SELECT … WHERE status = 'queued'`.

No new mechanism is required. The existing PostgreSQL-backed design already satisfies queued-task durability.

### 2. In-progress recovery (explicitly out of scope for v0.1)

A task with `status = 'in_progress'` may be interrupted by a process crash. The previous executor may still be running, or may already have performed external side effects (writes, API calls, etc.).

**v0.1 does not implement in-progress recovery.** When an orchestrator restarts, it MUST NOT automatically reset `in_progress` tasks to `queued`. Doing so could cause a task whose executor is still running (or has already performed side effects) to be re-claimed and executed again.

An operator may manually reset a stuck `in_progress` task to `queued` only after verifying:
- The previous executor is no longer running, AND
- No side effects from the interrupted execution remain outstanding.

This decision defers leases, heartbeat, and distributed locking to v0.2+.

### 3. Task lifecycle state machine

```
queued ──► in_progress ──► completed
                   │
                   └──► failed

completed and failed are terminal and immutable.
```

| State | Owner | Persisted by |
|---|---|---|
| `queued` | WorkloadService (enqueue) | PostgreSQL INSERT |
| `in_progress` | WorkerExecutionKernel (claim) | Kernel UPDATE |
| `completed` | WorkerExecutionKernel (complete) | Kernel UPDATE |
| `failed` | WorkerExecutionKernel (fail) | Kernel UPDATE |

**Terminal states (`completed`, `failed`)** are immutable. No component may transition a task out of either terminal state.

**No other states** (e.g., `blocked`, `pending`, `deferred`, `assigned`) exist in v0.1. The status constraint allows them in the DB but no code transitions to them.

### 4. Atomic ownership via claim

`WorkerExecutionKernel.execute()` uses a single atomic UPDATE with a status check before transitioning to `in_progress`:

```python
update_result = await session.execute(
    update(TaskModel)
    .where(TaskModel.id == task.id, TaskModel.status == "queued")
    .values(status="in_progress", started_at=func.now())
)
if update_result.rowcount == 0:
    raise TaskStatusError(...)
```

The `WHERE status = 'queued'` guard ensures only one orchestrator can claim a task. No separate claim table or lease row is needed. The loser's `rowcount == 0` causes a retry on the next poll cycle.

### 5. Concurrent claim race condition

Two orchestrators race to claim the same queued task. PostgreSQL's row-level locking ensures exactly one UPDATE succeeds. The loser receives `rowcount = 0` and must retry on the next poll.

No two orchestrators can both transition the same task to `in_progress` simultaneously.

### 6. Adapter failure / process exit during in_progress

When `adapter.execute()` throws an exception:
1. Kernel catches it and calls `kernel.fail(task_id, error)` — transitions task to `status = 'failed'`, persists `tasks.result = error_message`.
2. The kernel does NOT reset to `queued`. The task is terminal.

When a process exits unexpectedly (SIGKILL, OOM, hardware failure) while a task is `in_progress`:
1. The task remains `status = 'in_progress'` in PostgreSQL.
2. No automatic recovery occurs (see §2).
3. An operator must manually assess and reset if appropriate.

### 7. Checkpoint survival

A task may have a `checkpoint_ref` column pointing to a `task_checkpoints` row. This reference is set when a checkpoint is written during execution and is used to reload execution state on replay.

The `checkpoint_ref` column survives process restarts (it is a PostgreSQL column, not an in-memory reference). If a task is interrupted, the checkpoint row remains available for replay.

Replay (via `TaskSimulator`) reads `task.checkpoint_ref` to determine the replay starting point. The replayed task receives the same `profile_version` snapshot it was enqueued with.

### 8. Dependency integration

`WorkloadService.next_runnable()` already enforces dependency rules (Steps 8–9). A task whose dependencies are not all `completed` is never returned as runnable, regardless of its own status.

Restarting an orchestrator does not change dependency state. The dependency graph is persisted in PostgreSQL.

---

## Consequences

### Positive
- Tasks survive process restarts with no new mechanism.
- State machine is simple and enforceable: 4 states, 4 transitions.
- Atomic claim prevents double-execution without a separate lease table.
- Checkpoint references survive restarts, enabling replay.

### Negative
- In-progress tasks interrupted by crash are stuck until an operator intervenes.
- No automatic retry or failover in v0.1.

### Deferred to v0.2
- Leases with heartbeat/TTL
- Automatic recovery of `in_progress` tasks
- Distributed worker coordination
- Priority-based scheduling

---

## References

- ADR 0008 Addendum: WorkerProfile versioning
- ADR 0009: Task execution context — runtime resolves profile by snapshot
- ADR 0010: Worker execution kernel — orchestrate task lifecycle
- ADR 0011: Task simulation/replay — snapshot-driven historical context
- ADR 0014: Task dependency lifecycle
