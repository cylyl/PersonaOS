# ADR 0010 — Worker Execution Kernel

**Status:** Proposed (2026-10-08)
**Scope:** PersonaOS v0.1 (lifecycle orchestration)
**Supplements:** [ADR 0009 — WorkerExecutionContext](../adr/0009-worker-execution-context.md), [ADR 0008 — Thin Worker, six tables](0008-thin-worker-six-tables.md)

## Context

Step 4 (ADR 0009) gave us a stable input shape: `WorkerExecutionContext`. But nothing actually **executes** a task yet — `execution/adapters/*.py` are stubs, `workload/service.py` only enqueues, no lifecycle transitions happen.

Step 5 closes this gap by adding the **WorkerExecutionKernel**: a small component that orchestrates the lifecycle of a single task execution.

## Decision

### 1. Lifecycle (v0.1)

```
QUEUED → IN_PROGRESS → COMPLETED
              ↓
            FAILED
```

Three states (queued, in_progress, completed) plus an explicit failure terminal. The conceptual "RUNNING" maps to DB `in_progress` (the existing CHECK constraint uses `in_progress`).

No retries, leases, failover, or concurrent states yet — per ADR 0008 future evolution, those require execution-attempt modeling that the **failover acceptance test** will drive.

### 2. Kernel API

```python
result = await kernel.execute(task)
```

One method, one return type. The kernel owns the entire lifecycle:
validate state → resolve context → transition to in_progress → emit `task.claimed` → load checkpoint → call `adapter.execute` → persist result → transition to completed|failed → emit `task.completed|task.failed`.

### 3. Adapter abstraction

The kernel does NOT know about OpenClaw. It uses the `Adapter` Protocol:

```python
class Adapter(Protocol):
    async def execute(
        self,
        context: WorkerExecutionContext,
        checkpoint: Any | None,
    ) -> Result: ...
```

This keeps the kernel independent of the agent engine and leaves room for future adapters:

```
WorkerExecutionKernel
   ↓
ExecutionAdapter (Protocol)
   ↓
OpenClawAdapter (now stub; full impl deferred)
CodexAdapter (future)
LocalLLMAdapter (future)
SimulationAdapter (Step 6)
HumanAdapter (future)
```

### 4. Result type

```python
@dataclass
class Result:
    status: str                 # "completed" | "failed"
    output: dict | None = None
    error: str | None = None
    new_checkpoint: dict | None = None
```

The "needs_input" status from the original base.py TODO is **deferred** — it's not in the Step 5 lifecycle.

### 5. Adapter exceptions

The Adapter MAY raise. The kernel catches the exception and converts it to `Result(status="failed", error=...)`. The kernel itself never crashes.

### 6. Events emitted

Per ADR 0006, the kernel emits:

| Event | When |
|---|---|
| `task.claimed` | queued → in_progress transition |
| `task.checkpointed` | When `result.new_checkpoint` is persisted |
| `task.completed` | Successful finish |
| `task.failed` | Failed finish (adapter returned failed OR raised) |

`task.created` is emitted by `WorkloadService.enqueue` (Step 4).

### 7. State persistence — three phases

```
Phase 1:  queued → in_progress + task.claimed       (one transaction)
Phase 2:  (adapter executes — no DB transaction)
Phase 3a: in_progress → completed + result + event  (one transaction)
   or
Phase 3b: in_progress → failed + last_error + event (one transaction)
```

Each phase commits independently. A crash between Phase 1 and Phase 3 leaves the task `in_progress`; recovery is **deferred**.

### 8. No retry/lease/failover

`retry_count` stays at 0 on failure. `max_retries` is ignored. The schema fields `execution_node`, `attempt`, `lease_expires_at` are **not added** — per ADR 0008 future evolution, the failover acceptance test drives those.

## Invariants

| # | Invariant | Enforcement |
|---|---|---|
| 1 | Task enters execution in `queued` state | Kernel raises `TaskStatusError` otherwise |
| 2 | Context is resolved from `task.profile_version` | Kernel takes `resolve_context` callable, calls once |
| 3 | Adapter receives the frozen context | `WorkerExecutionContext` is `@dataclass(frozen=True)`; passed by reference |
| 4 | Successful execution → `status="completed"` | Kernel transitions after `Result(status="completed")` |
| 5 | Failed execution → `status="failed"` | Adapter returns failed OR raises; kernel converts either way |
| 6 | `result.output` persisted on `task.result` | Kernel writes it during Phase 3a |
| 7 | `result.error` persisted on `task.last_error` | Kernel writes it during Phase 3b |
| 8 | Failed task does NOT auto-retry | `retry_count` unchanged; `max_retries` ignored |
| 9 | Kernel is independent of OpenClaw | `Adapter` Protocol abstraction; no OpenClaw imports in `kernel.py` |
| 10 | Events emitted for every state transition | Kernel emits `task.claimed/completed/failed/checkpointed` |

## What's NOT in Step 5 (deferred)

Per ADR 0008 future evolution:

- Retry semantics (`retry_count` flow, `max_retries` escalation)
- Lease tracking (`tasks.execution_node`, `tasks.attempt`, `tasks.lease_expires_at`)
- Failover / resume after node failure
- Supervisor / long-running daemon
- Scheduling (FIFO + priority + skill-match)
- Concurrency (workers running multiple tasks in parallel)
- Multi-node execution
- Simulation / replay (Step 6 — uses the kernel + a `SimulationAdapter`)

## Migration path

| Sub-step | Deliverable |
|---|---|
| 5a | This ADR + spec + acceptance tests (this commit; tests are skipped until 5b) |
| 5b | `Result` dataclass + `Adapter` Protocol update + `WorkerExecutionKernel` + checkpoint module + events store |
| 5c | Step 5 prep tests unskip and pass; commit Step 5 as a single feature |
| 5d | Verification — 34 (Steps 3-4) + 10 (Step 5) = **44 tests green, 0 skipped, 0 failed** |

## References

- [ADR 0008 — Thin Worker model, six tables for v0.1](0008-thin-worker-six-tables.md)
- [ADR 0008 Addendum — Versioned WorkerProfile + Immutable Task Snapshots](0008-addendum-worker-profile-versioning.md)
- [ADR 0009 — WorkerExecutionContext](../adr/0009-worker-execution-context.md)
- Spec: [`docs/specs/worker-execution-kernel.md`](../specs/worker-execution-kernel.md)
- Tests: `tests/integration/test_worker_execution_kernel.py`
