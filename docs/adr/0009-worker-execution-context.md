# ADR 0009 — WorkerExecutionContext: Task Runtime Resolves Profile by Snapshot

**Status:** Proposed (2026-10-08)
**Scope:** PersonaOS v0.1 (closes the loop on Step 3 / ADR 0008 addendum)
**Supplements:** [ADR 0008 — Thin Worker, six tables](0008-thin-worker-six-tables.md), [ADR 0008 Addendum — WorkerProfile versioning](0008-addendum-worker-profile-versioning.md)

## Context

Step 3 (ADR 0008 addendum + migration 0002 + ProfileRegistry) gave us two pieces:

1. **Snapshot mechanism** — `tasks.profile_version` is captured at enqueue and never updated.
2. **Resolution primitive** — `ProfileRegistry.get_profile_version(worker_id, version)` loads any historical version.

**The gap**: nothing in the runtime reads `task.profile_version` and builds an execution context from it. The snapshot column is dead code — every existing execution path implicitly trusts `worker.current_profile_version`, which violates the core property:

> A task's historical identity must never depend on the worker's current state.

Concretely:

- A task enqueued under v2 → worker activates v3 → if execution uses `worker.current_profile_version`, the task runs with v3 (silently). Audit/replay impossible.
- An activation racing with an in-flight task makes the bug worse: a v3 activation mid-execution produces mixed-version behavior.

The Step 3 test suite proves the *column* preserves the snapshot. Step 4 must prove the *runtime* consumes the snapshot.

## Decision

### 1. Add `WorkerExecutionContext`

A frozen dataclass in `src/personaos/execution/context.py` that bundles the three things execution needs:

```python
@dataclass(frozen=True)
class WorkerExecutionContext:
    worker: Worker              # identity + ops fields
    profile: WorkerProfile      # the SNAPSHOT — exactly what was active at enqueue
    task: Task                  # the task being executed
```

The `profile` field is the **historical contract**. It was loaded by `ProfileRegistry.get_profile_version(worker.id, task.profile_version)` — never from `worker.current_profile_version`.

### 2. ProfileRegistry gets `resolve_execution_context`

```python
async def resolve_execution_context(self, task: Task) -> WorkerExecutionContext:
    """Load worker + snapshot profile, return a frozen context.

    NEVER reads worker.current_profile_version. ALWAYS reads task.profile_version.
    """
    if not task.assigned_worker:
        raise ProfileError(
            f"task {task.id} has no assigned_worker; cannot resolve context"
        )
    worker = await self.get_worker(task.assigned_worker)
    profile = self.get_profile_version(worker.id, task.profile_version)
    return WorkerExecutionContext(worker=worker, profile=profile, task=task)
```

This is the **only** way the runtime gets a profile.

### 3. Adapter signature changes

Before:
```python
async def execute(self, task: Task, checkpoint: Checkpoint | None) -> Result: ...
```

After:
```python
async def execute(self, context: WorkerExecutionContext, checkpoint: Checkpoint | None) -> Result: ...
```

The adapter receives the full context. `context.task` is the task; `context.worker` is the worker identity; `context.profile` is the snapshot profile (persona, instructions, skills, permissions, runtime config).

The signature documents the contract: there is no way for an adapter to silently grab `worker.current_profile_version` because that field isn't on the context.

### 4. `WorkloadService.enqueue` snapshots atomically

```python
async def enqueue(self, *, worker_id: str, title: str, objective: str, input: dict, ...) -> Task:
    """Snapshot workers.current_profile_version atomically with the INSERT."""
    async with self._session.begin():
        # SELECT ... FOR UPDATE on the worker row, in the same transaction
        result = await self._session.execute(
            select(WorkerModel.current_profile_version)
            .where(WorkerModel.id == worker_id)
            .with_for_update()
        )
        profile_version = result.scalar_one()
        # INSERT task with profile_version=profile_version
        ...
    return Task.from_orm(task_row)
```

`SELECT ... FOR UPDATE` on the worker row + INSERT in the same transaction makes the snapshot atomic with respect to concurrent activations. If an activation is mid-flight, the enqueue waits; it then reads either the old or new version, consistently.

## Invariants

| # | Invariant | Enforcement |
|---|---|---|
| 1 | Tasks enqueued after activation N reference version N | `WorkloadService.enqueue` reads `current_profile_version` with `FOR UPDATE` atomically |
| 2 | Tasks enqueued before activation N+1 still reference N | `tasks.profile_version` is never updated after INSERT |
| 3 | Execution always uses `task.profile_version` | `resolve_execution_context` is the only path; adapters receive `context` |
| 4 | Historical profiles remain resolvable | `ProfileRegistry.get_profile_version` returns any version on disk |
| 5 | Execution context is explicit (no implicit state) | `WorkerExecutionContext` is the input to `adapter.execute` |

## What's NOT in Step 4 (deferred)

| Deferred | Reason |
|---|---|
| `tasks.execution_node`, `tasks.attempt`, `tasks.lease_expires_at` | ADR 0008 — failover acceptance test drives the shape |
| Retry semantics (`retry_count` flow, max_retries escalation) | v0.2 — after failover |
| LLM provider selection / model routing | Step 5+ |
| Actual agent loop | Step 5+ |
| Profile inheritance / composition | Separate concern |
| Canary deployment | Separate concern |
| Profile diffing UI | Out of scope |
| Pydantic-based YAML validation | Manual for v0.1 |

## Consequences

**Positive:**

- Snapshot semantics are no longer dead code — they actually drive execution.
- Activation can change `current_profile_version` without affecting in-flight tasks.
- Audit/replay becomes possible: load the historical task, resolve its `profile_version`, load that exact YAML.
- The adapter signature documents the snapshot contract in code: `execute(context, checkpoint)` says "the context is the truth."
- Step 6 (simulation/replay) has a clean unit of replay: a historical task + its context.

**Negative:**

- One more type to maintain (`WorkerExecutionContext`).
- Adapter signature change is a v0.1 → v0.1 break for any adapter that exists today (none — they're all stubs).
- The atomic enqueue requires a transaction; small overhead but well-understood.

## Migration path

| Sub-step | Deliverable | Status |
|---|---|---|
| 4a | This ADR + spec + acceptance tests (skipped until 4b) | **this commit** |
| 4b | `WorkerExecutionContext`, `WorkloadService.enqueue`, `ProfileRegistry.resolve_execution_context`, `Task` dataclass, `Task.from_orm` | next commit |
| 4c | Adapter signature update (`base.py`, `openclaw.py`) | bundled with 4b |
| 4d | Step 4 prep tests unskip and pass | bundled with 4b |

## References

- [ADR 0008 — Thin Worker model, six tables for v0.1](0008-thin-worker-six-tables.md)
- [ADR 0008 Addendum — Versioned WorkerProfile + Immutable Task Snapshots](0008-addendum-worker-profile-versioning.md)
- Spec: [`docs/specs/task-execution-context.md`](../specs/task-execution-context.md)
- Spec: [`docs/specs/worker-profile.md`](../specs/worker-profile.md)
- Tests: `tests/integration/test_task_execution_context.py`
