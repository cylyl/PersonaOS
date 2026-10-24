# ADR 0023: Worker Concurrency Pool & Workload Capacity Enforcement

**Status:** Accepted (Step 20 Implementation Phase)
**Date:** 2026-10-23
**Deciders:** PersonaOS Architecture Team
**Consulted:** Scheduler, Workload, Kernel, Execution Runner
**Informed:** Operators, Developers

---

## Context & Problem Statement

PersonaOS operates on the founding thesis: **“Compute is replaceable; the worker is persistent.”**
In Steps 1 to 19, the framework established durable worker identities, versioned profile snapshots, SQL-based capability and skill matching (ADR 0019), security policy enforcement (ADR 0020), resilient checkpoint resumption (ADR 0021), and execution context assembly (ADR 0022).

However, an examination of [`WorkloadScheduler`](file:///root/orca/workspaces/PersonaOS/yabby/src/personaos/workload/scheduler.py) and [`WorkerRunner`](file:///root/orca/workspaces/PersonaOS/yabby/src/personaos/execution/runner.py) reveals two critical concurrency limitations:

1. **Unenforced Capacity in Scheduler:**
   - Both `WorkerModel.max_concurrent_tasks` and `WorkerProfile.max_concurrent_tasks` exist in the domain model.
   - However, `WorkloadScheduler._CANDIDATE_SELECTION_SQL` does not check how many tasks are currently `in_progress` for the worker. A worker configured with `max_concurrent_tasks: 1` can have multiple tasks claimed if multiple orchestrators or polling loops query for work, causing worker overload and violating profile SLAs.
2. **Serial Execution in WorkerRunner:**
   - `WorkerRunner` and `WorkerOrchestrator` operate strictly serially in a single-threaded loop (`process_one()`). Even when a worker is configured with `max_concurrent_tasks > 1` (e.g. 2, 4), the runner can only execute one task at a time.
   - Concurrently executing tasks on a compute node requires isolated SQLAlchemy sessions per task to prevent state corruption, concurrent transaction overlap, and heartbeat starvation.

---

## Decision

We will implement **Worker Concurrency Pooling and Capacity Enforcement** across the scheduler, orchestrator, and runner layers:

### 1. SQL-Level Capacity Gating in WorkloadScheduler
Extend `_CANDIDATE_SELECTION_SQL` in `src/personaos/workload/scheduler.py`:
- Count active `in_progress` tasks assigned to `:worker_id`:
  ```sql
  AND (
      SELECT COUNT(*)
      FROM tasks active_t
      WHERE active_t.assigned_worker = :worker_id
        AND active_t.status = 'in_progress'
  ) < (
      SELECT COALESCE(w.max_concurrent_tasks, 1)
      FROM workers w
      WHERE w.id = :worker_id
  )
  ```
- If active tasks reach or exceed `max_concurrent_tasks`, the worker is saturated and returns `None` immediately, preventing over-allocation.

### 2. Session Isolation for Concurrent Executions
- In `WorkerExecutionKernel` and `WorkerOrchestrator`, concurrent tasks must not share a single `AsyncSession`.
- `WorkerOrchestrator` and `WorkerRunner` accept `session_factory: async_sessionmaker[AsyncSession]`. For each task execution, a dedicated session is provided so concurrent transactions, heartbeats, and checkpoint writes remain completely isolated.

### 3. Worker Concurrency Pool in WorkerOrchestrator & WorkerRunner
- In `WorkerOrchestrator`:
  - Support concurrent task execution tracking via an active task set (`set[asyncio.Task]`).
  - Read `max_concurrent_tasks` from the worker record (or configured parameter).
  - When concurrency slots are available (`len(active_tasks) < max_concurrent_tasks`), continue polling and dispatching tasks without blocking the orchestrator loop.
  - When capacity is reached, await the completion of at least one in-flight task before claiming new work.
  - On `stop()`, gracefully await all active tasks in the pool to reach completion or checkpoint yield.

### 4. Zero Database Migrations
- All required columns (`workers.max_concurrent_tasks`, `tasks.status`, `tasks.assigned_worker`) exist in the v0.1 schema. No DDL alterations are required.

---

## Preserved Invariants

1. **Kernel Claim Authority**: Atomic claims remain generation-fenced inside `WorkerExecutionKernel.execute()`.
2. **PostgreSQL as Source of Truth**: Active task count and worker capacity are evaluated directly in SQL against the durable store.
3. **Task Isolation**: An exception or failure in one concurrent task does not abort, cancel, or poison sibling concurrent tasks.
4. **Heartbeat Independence**: Each concurrent task runs its own leased heartbeat loop independently.

---

## Consequences

### Positive
- Enforces worker capacity SLAs defined in versioned worker profiles.
- Enables multi-task parallel execution on compute nodes without dropping lease heartbeats.
- Prevents worker starvation and unfair task hoarding.

### Trade-offs
- Multiple concurrent tasks require multiple database connections from the connection pool. Handled cleanly via SQLAlchemy session factories and bounded `max_concurrent_tasks`.
