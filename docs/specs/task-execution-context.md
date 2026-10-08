# Task Execution Context Specification

**Status:** Draft v0.1 (2026-10-08)
**Companion to:** [ADR 0009 — WorkerExecutionContext](../adr/0009-worker-execution-context.md)

## 1. Concept

A `WorkerExecutionContext` bundles the three things a runtime adapter needs to execute a task:

```
WorkerExecutionContext (frozen)
├── worker       Worker          # identity + ops fields (id, name, role, status, runtime, ...)
├── profile      WorkerProfile   # the SNAPSHOT — exactly what was active at enqueue
└── task         Task            # the task being executed
```

The `profile` field is the **historical contract**. It is loaded by
`ProfileRegistry.get_profile_version(worker.id, task.profile_version)` —
never from `worker.current_profile_version`.

## 2. Why a context type?

Three reasons:

1. **Document the snapshot contract in code.** `adapter.execute(context, checkpoint)` says: the context is the truth. There is no way for an adapter to silently read `worker.current_profile_version` because that field is not on the context.
2. **Make audit/replay trivial.** `context.profile` is the exact YAML the task ran with. Storing the context next to the task event gives a complete audit trail.
3. **Prepare for Step 6 (simulation/replay).** A historical task + historical profile + historical input is enough to re-run. The context is the unit of replay.

## 3. The resolution path

```
Task creation
   ↓
[WorkloadService.enqueue snapshots
  workers.current_profile_version via SELECT ... FOR UPDATE]
   ↓
Task persisted (status=queued, profile_version=N)
   ↓
Worker picks up task via scheduler.claim_next(worker_id, node_id)
   ↓
ProfileRegistry.resolve_execution_context(task)
   ├── worker   = ProfileRegistry.get_worker(task.assigned_worker)
   └── profile  = ProfileRegistry.get_profile_version(worker.id, task.profile_version)
                                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                                snapshot — NOT worker.current_profile_version
   ↓
WorkerExecutionContext(worker, profile, task)
   ↓
adapter.execute(context, checkpoint) -> Result
   ↓
checkpoint.write(task, result.state)
   ↓
workload.complete(task, result=result)
```

The only way to load a profile for execution is via `resolve_execution_context`. There is no "load current profile" code path in the runtime.

## 4. API

### 4.1 `WorkerExecutionContext` (frozen dataclass)

```python
# src/personaos/execution/context.py
from dataclasses import dataclass
from personaos.domain.task import Task
from personaos.domain.worker import Worker, WorkerProfile


@dataclass(frozen=True)
class WorkerExecutionContext:
    """Bundle of worker + snapshot profile + task.

    The profile is loaded from task.profile_version (NOT worker.current_profile_version).
    Adapters receive this context, never a bare task, so the snapshot contract is in the type.
    """
    worker: Worker
    profile: WorkerProfile
    task: Task
```

### 4.2 `ProfileRegistry.resolve_execution_context`

```python
# src/personaos/registry/profile.py (added)
async def resolve_execution_context(self, task: Task) -> WorkerExecutionContext:
    """Load worker + snapshot profile, return a frozen context.

    NEVER uses worker.current_profile_version. ALWAYS uses task.profile_version.
    Raises ProfileError if the task has no assigned_worker.
    Raises WorkerNotFoundError if the worker is gone.
    Raises ProfileVersionNotFoundError if the snapshot version is missing on disk.
    """
    if not task.assigned_worker:
        raise ProfileError(
            f"task {task.id} has no assigned_worker; cannot resolve context"
        )
    worker = await self.get_worker(task.assigned_worker)
    profile = self.get_profile_version(worker.id, task.profile_version)
    return WorkerExecutionContext(worker=worker, profile=profile, task=task)
```

### 4.3 `WorkloadService.enqueue`

```python
# src/personaos/workload/service.py (added)
async def enqueue(
    self,
    *,
    worker_id: str,
    title: str,
    objective: str,
    input: dict,
    required_skills: list[str] | None = None,
    required_capabilities: list[str] | None = None,
    priority: str = "medium",
    type: str | None = None,
    dependencies: list[str] | None = None,
    deadline: datetime | None = None,
    max_retries: int = 3,
) -> Task:
    """Snapshot workers.current_profile_version atomically with the INSERT.

    The snapshot uses SELECT ... FOR UPDATE on the worker row + INSERT in the
    same transaction. This guarantees:
      - ProfileVersion snapshot is consistent with the worker state at enqueue
      - No concurrent activation can interleave between snapshot and INSERT

    Raises WorkerNotFoundError if worker_id doesn't exist.
    """
    async with self._session.begin():
        result = await self._session.execute(
            select(WorkerModel.current_profile_version)
            .where(WorkerModel.id == worker_id)
            .with_for_update()
        )
        profile_version = result.scalar_one_or_none()
        if profile_version is None:
            raise WorkerNotFoundError(f"worker {worker_id} not found")

        task_row = TaskModel(
            id=str(uuid.uuid4()),
            title=title,
            objective=objective,
            input=input,
            assigned_worker=worker_id,
            profile_version=profile_version,
            required_skills=required_skills or [],
            required_capabilities=required_capabilities or [],
            priority=priority,
            type=type,
            dependencies=dependencies or [],
            deadline=deadline,
            max_retries=max_retries,
            status="queued",
        )
        self._session.add(task_row)
        # task.created event emitted here (per ADR 0006)
    return Task.from_orm(task_row)
```

### 4.4 Adapter signature

```python
# src/personaos/execution/adapters/base.py
from typing import Protocol
from personaos.execution.context import WorkerExecutionContext


class Adapter(Protocol):
    """Runtime adapter contract. Receives a frozen execution context."""

    async def execute(
        self,
        context: WorkerExecutionContext,
        checkpoint: Checkpoint | None,
    ) -> Result: ...
```

## 5. Acceptance criteria

Mapped to ADR 0009 invariants:

| # | Criterion | Test |
|---|---|---|
| 1 | Enqueue snapshots `workers.current_profile_version` atomically | `test_enqueue_snapshots_current_profile_version` |
| 2 | Activation after enqueue doesn't affect the task's snapshot | `test_activation_after_enqueue_does_not_change_task_snapshot` |
| 3 | `resolve_execution_context` loads `task.profile_version`, not `worker.current_profile_version` | `test_resolve_uses_task_snapshot_not_worker_current` |
| 4 | Historical profiles remain resolvable after multiple activations | `test_historical_profile_remains_resolvable` |
| 5 | `WorkerExecutionContext` is a frozen bundle of worker + profile + task | `test_context_bundles_worker_profile_task` |
| 6 | No failover / retry / lease fields introduced | `test_no_v0_2_columns_in_migration_0002` |

## 6. What's NOT in Step 4 (deferred)

Per ADR 0008 future evolution:

- `tasks.execution_node` — current runtime location (vs the historical `worker_nodes` binding)
- `tasks.attempt` — retry counter
- `tasks.lease_expires_at` — when the scheduler should reclaim

Per Step 4 scope (this ADR):

- LLM provider selection / model routing (Step 5+)
- Actual agent loop (Step 5+)
- Profile inheritance / composition (separate)
- Canary deployment (separate)
- Profile diffing UI (separate)
- Pydantic-based YAML validation (v0.2)

## 7. Implementation order (Step 4 sub-steps)

| Sub-step | Deliverable |
|---|---|
| 4a | This spec + ADR 0009 + acceptance tests (this commit, tests are skipped until 4b) |
| 4b | `WorkerExecutionContext` + `Task` dataclass + `WorkloadService.enqueue` + `ProfileRegistry.resolve_execution_context` + adapter signature update |
| 4c | Step 4 prep tests unskip and pass; commit Step 4 as a single feature |
| 4d | Verification — 27 (Step 3) + 6 (Step 4) = 33 tests green |

## 8. Example flow

```python
# 1. Worker exists with v1 active
reg = ProfileRegistry(session, profiles_dir)
await reg.register_worker("worker-devops-001", v1_profile)

# 2. Create + activate v2 (cautious → more cautious)
await reg.create_profile_version("worker-devops-001", v2_profile)
await reg.activate_profile_version("worker-devops-001", 2)

# 3. Enqueue task — snapshot captures v2
wl = WorkloadService(session)
task_a = await wl.enqueue(
    worker_id="worker-devops-001",
    title="deploy service",
    objective="deploy v1.4.2 to staging",
    input={"service": "api", "version": "1.4.2"},
)
assert task_a.profile_version == 2  # snapshot

# 4. Activate v3 — worker's current is now 3
await reg.create_profile_version("worker-devops-001", v3_profile)
await reg.activate_profile_version("worker-devops-001", 3)

# 5. Enqueue task_b — snapshot captures v3
task_b = await wl.enqueue(worker_id="worker-devops-001", title="B", objective="B", input={})
assert task_b.profile_version == 3

# 6. Scheduler claims task_a — runtime resolves via snapshot, NOT current
context = await reg.resolve_execution_context(task_a)
assert context.profile.version == 2  # NOT 3
assert context.profile.persona.autonomy == v2_profile.persona.autonomy  # v2's value

# 7. Adapter executes with the frozen context
result = await adapter.execute(context, checkpoint)
```
