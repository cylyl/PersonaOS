# Worker Execution Kernel Specification

**Status:** Draft v0.1 (2026-10-08)
**Companion to:** [ADR 0010 — Worker Execution Kernel](../adr/0010-worker-execution-kernel.md)

## 1. Concept

The `WorkerExecutionKernel` orchestrates the execution lifecycle of a single task:

```
QUEUED → IN_PROGRESS → COMPLETED
              ↓
            FAILED
```

The kernel is independent of any specific agent engine. It uses the `Adapter` Protocol to delegate execution to a swappable backend.

## 2. Kernel API

```python
result = await kernel.execute(task)
```

**Inputs:**
- `task`: a `Task` dataclass (must be in `queued` state)

**Returns:**
- `Result` (the adapter's return value, possibly converted from an exception)

**Raises:**
- `TaskStatusError` if `task.status != "queued"`

## 3. Lifecycle phases

### Phase 1 — Validate state

```python
if task.status != "queued":
    raise TaskStatusError(
        f"task {task.id} is in status {task.status!r}; expected 'queued'"
    )
```

### Phase 2 — Resolve context (snapshot semantics)

```python
context = await resolve_context(task)
```

The kernel takes `resolve_context: Callable[[Task], Awaitable[WorkerExecutionContext]]` and calls it **once**. The context is frozen and passed unchanged to the adapter.

### Phase 3 — Transition to in_progress + claim event

```python
update(TaskModel).where(TaskModel.id == task.id).values(
    status="in_progress",
    started_at=func.now(),
)
emit_task_event(task_id=task.id, event_type="task.claimed", actor_id="kernel")
commit()
```

### Phase 4 — Load checkpoint (if any)

```python
checkpoint_state = await checkpoint_load(session, task.checkpoint_ref)
# returns dict | None
```

### Phase 5 — Execute via adapter (no DB transaction held)

```python
try:
    result = await adapter.execute(context=context, checkpoint=checkpoint_state)
except Exception as e:
    result = Result(status="failed", error=f"{type(e).__name__}: {e}")
```

The adapter MAY raise; the kernel converts to `Result(status="failed")`.

### Phase 6a — Persist completed result

```python
checkpoint_ref = task.checkpoint_ref
if result.new_checkpoint is not None:
    checkpoint_ref = await checkpoint_write(session, task_id, result.new_checkpoint)
    emit_task_event(task_id, "task.checkpointed", actor_id="kernel")

update(TaskModel).where(TaskModel.id == task.id).values(
    status="completed",
    completed_at=func.now(),
    result=result.output,
    checkpoint_ref=checkpoint_ref,
)
emit_task_event(task_id, "task.completed", actor_id="kernel")
commit()
```

### Phase 6b — Persist failed result

```python
update(TaskModel).where(TaskModel.id == task.id).values(
    status="failed",
    completed_at=func.now(),
    result={"error": result.error} if result.error else None,
    last_error=result.error,
)
emit_task_event(task_id, "task.failed", actor_id="kernel")
commit()
```

## 4. Adapter abstraction

```python
class Adapter(Protocol):
    async def execute(
        self,
        context: WorkerExecutionContext,
        checkpoint: Any | None,
    ) -> Result: ...
```

The kernel does **not** know about OpenClaw. The Adapter may be:

| Adapter | Status |
|---|---|
| `OpenClawAdapter` | Stub (full impl deferred) |
| `CodexAdapter` | Future |
| `LocalLLMAdapter` | Future |
| `SimulationAdapter` | Step 6 (uses the kernel + replay) |
| `HumanAdapter` | Future |

## 5. Result type

```python
@dataclass
class Result:
    status: str                 # "completed" | "failed"
    output: dict | None = None
    error: str | None = None
    new_checkpoint: dict | None = None
```

The "needs_input" status is **deferred** — not in the Step 5 lifecycle.

## 6. Checkpoint module

```python
async def load(session: AsyncSession, ref: str | None) -> dict | None
async def write(session: AsyncSession, task_id: str, state: dict) -> str
```

Storage: `task_checkpoints` table (JSONB state). The `checkpoint_ref` on the task is the linkage.

## 7. Events store

```python
async def emit_task_event(
    session: AsyncSession,
    *,
    task_id: str,
    event_type: str,
    actor_id: str,
    payload: dict | None = None,
) -> None
```

v0.1 event types (CHECK-constrained): `task.created`, `task.claimed`, `task.checkpointed`, `task.completed`, `task.failed`.

## 8. Acceptance criteria

| # | Criterion | Test |
|---|---|---|
| 1 | Queued task executes with its snapshot | `test_queued_task_executes_with_its_snapshot` |
| 2 | RUNNING → COMPLETED | `test_running_to_completed_persists_result` |
| 3 | RUNNING → FAILED | `test_running_to_failed_persists_error` |
| 4 | Execution context passed unchanged to adapter | `test_queued_task_executes_with_its_snapshot` (verifies `captured_context.profile.version`) |
| 5 | Adapter failure becomes task failure | `test_adapter_exception_becomes_task_failure` |
| 6 | Successful result persisted | `test_running_to_completed_persists_result` |
| 7 | Failed result/error persisted | `test_running_to_failed_persists_error` |
| 8 | Current worker profile irrelevant after resolution | `test_queued_task_executes_with_its_snapshot` (activates v3 between enqueue and execute) |
| 9 | No retry/lease behavior introduced | `test_no_retry_on_failure` |
| 10 | Existing 34 tests remain green | (verified by full suite run, not a single test) |

## 9. What's NOT in Step 5 (deferred)

Per ADR 0008 future evolution:

- Retry semantics (`retry_count`, `max_retries`)
- Lease tracking (`tasks.execution_node`, `tasks.attempt`, `tasks.lease_expires_at`)
- Failover / resume
- Supervisor / daemon
- Scheduling / queue daemon
- Concurrency / multi-task per worker
- Multi-node execution
- Simulation / replay (Step 6)

## 10. Example flow

```python
# Set up
reg = ProfileRegistry(session, profiles_dir)
await reg.register_worker("w1", v1)
await reg.create_profile_version("w1", v2)
await reg.activate_profile_version("w1", 2)

wl = WorkloadService(session)
task = await wl.enqueue(worker_id="w1", title="deploy", objective="...", input={})
assert task.profile_version == 2

# Activate v3 — worker.current = 3
await reg.create_profile_version("w1", v3)
await reg.activate_profile_version("w1", 3)

# Execute
adapter = MyAdapter()  # any Adapter implementation
kernel = WorkerExecutionKernel(
    session=session,
    adapter=adapter,
    resolve_context=reg.resolve_execution_context,
)
result = await kernel.execute(task)

assert result.status == "completed"

# Adapter received the v2 profile (snapshot), NOT v3 (current)
assert adapter.captured_context.profile.version == 2
assert adapter.captured_context.profile.persona.autonomy == v2.persona.autonomy

# DB state
task_row = await session.get(TaskModel, task.id)
assert task_row.status == "completed"
assert task_row.completed_at is not None
assert task_row.result == result.output
```

## 11. Implementation order

| Sub-step | Deliverable |
|---|---|
| 5a | This spec + ADR 0010 + acceptance tests (tests skip until 5b) |
| 5b | `Result` + `Adapter` Protocol update + `WorkerExecutionKernel` + checkpoint module + events store |
| 5c | Run full suite; **44 tests green, 0 skipped, 0 failed** |
| 5d | Commit as one atomic Step 5 feature |
