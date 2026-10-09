# Durable Workload Queue — Spec

**Spec status:** Draft
**Parent ADR:** ADR 0015

---

## Goals

1. **Queued tasks survive process restarts** — no in-memory queue; all state in PostgreSQL.
2. **Orchestrator restart rediscoverers queued work** — `next_runnable()` reads from PostgreSQL.
3. **State transitions are durable** — every transition (queued→in_progress, in_progress→completed/failed) is committed to PostgreSQL before the next phase begins.
4. **Terminal states are immutable** — `completed` and `failed` cannot be re-claimed or overwritten.
5. **Atomic claim prevents double-execution** — the kernel's `UPDATE WHERE status='queued'` is the sole claim mechanism; no in-process lock needed.
6. **Checkpoint refs survive restarts** — `task.checkpoint_ref` is a PostgreSQL column, not an in-memory pointer.
7. **Dependency graph survives restarts** — persisted in PostgreSQL; unchanged by restart.
8. **In-progress recovery is out of scope for v0.1** — crash policy is operator-managed.

---

## Architecture

### No in-memory queue

`WorkloadService.next_runnable()` queries PostgreSQL directly. The orchestrator has no in-memory worklist. Each `process_one()` call does a fresh `SELECT … WHERE status='queued'` (with dependency check).

```
Orchestrator.process_one()
  → WorkloadService.next_runnable()      ← fresh PostgreSQL SELECT
  → WorkerExecutionKernel.execute()
      → atomic UPDATE WHERE status='queued'  ← kernel Phase 3 claim
```

### State machine

```
queued ──(kernel Phase 3)──► in_progress ──(adapter returns)──► completed
                               │
                               └──► failed
```

| State | Who transitions | Persisted by |
|---|---|---|
| `queued` | `WorkloadService.enqueue()` | PostgreSQL INSERT |
| `in_progress` | `Kernel.execute()` Phase 3 | PostgreSQL UPDATE |
| `completed` | `Kernel._finish_completed()` | PostgreSQL UPDATE |
| `failed` | `Kernel._finish_failed()` | PostgreSQL UPDATE |

`completed` and `failed` are **terminal and immutable**. No code path transitions out of either state.

### Atomic claim

Kernel Phase 3:

```python
update_result = await session.execute(
    update(TaskModel)
    .where(TaskModel.id == task.id, TaskModel.status == "queued")
    .values(status="in_progress", started_at=func.now())
)
if update_result.rowcount == 0:
    raise TaskStatusError(...)
```

- `WHERE status='queued'` is the concurrency guard.
- Loser gets `rowcount == 0` → `TaskStatusError` → orchestrator catches it as claim conflict, moves on.
- No separate lease table needed.

### Checkpoint survival

`task.checkpoint_ref` is a nullable `TEXT` column. When the adapter returns `Result(new_checkpoint=state)`:

```python
checkpoint_ref = await checkpoint_write(session, task_id=task.id, state=state)
await session.execute(
    update(TaskModel)
    .where(TaskModel.id == task.id)
    .values(status="completed", checkpoint_ref=checkpoint_ref, ...)
)
```

On restart, `kernel.execute()` calls `checkpoint_load(session, task.checkpoint_ref)` — if non-null, the state dict is passed to the adapter as the `checkpoint` argument.

### Dependency integration

`next_runnable()` enforces that a task is only returned when all entries in `task.dependencies` have `status='completed'`. This is evaluated in PostgreSQL on every call — no in-memory state.

---

## v0.1 Out of Scope

- Leases / heartbeat / TTL for `in_progress` tasks
- Automatic recovery of interrupted `in_progress` tasks
- Distributed worker coordination (multiple orchestrators racing)
- Priority-based scheduling beyond `created_at ASC, id ASC` FIFO

---

## Test Matrix

| # | Test | Scope |
|---|---|---|
| 1 | `test_queued_task_survives_session_recreation` | next_runnable after new session |
| 2 | `test_queued_task_is_discovered_after_orchestrator_restart` | next_runnable after orchestrator object recreated |
| 3 | `test_claim_transition_is_persisted` | queued→in_progress survives new session |
| 4 | `test_completed_transition_is_persisted` | in_progress→completed survives new session |
| 5 | `test_failed_transition_is_persisted` | in_progress→failed survives new session |
| 6 | `test_terminal_task_cannot_be_claimed_again` | completed/failed cannot be re-claimed |
| 7 | `test_concurrent_claim_has_one_winner` | two kernels race, one wins |
| 8 | `test_dependency_rules_survive_session_recreation` | dep satisfaction unchanged after restart |
| 9 | `test_checkpoint_reference_survives_session_recreation` | checkpoint_ref readable after restart |
| 10 | `test_interrupted_in_progress_task_follows_defined_recovery_policy` | **SKIP** — out of scope for v0.1 (ADR 0015 §2) |

---

## Verification

```bash
cd /root/.openclaw/workspace/projects/PersonaOS
pytest tests/integration/test_durable_workload_queue.py -v
# Expected: 9 passed, 1 skipped
```
