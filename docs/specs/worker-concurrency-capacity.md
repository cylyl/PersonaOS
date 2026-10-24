# Specification: Worker Concurrency Pool & Workload Capacity Enforcement

**Module:** `personaos.workload.scheduler`, `personaos.orchestrator.worker`, `personaos.execution.runner`
**Related ADR:** ADR 0023, ADR 0003, ADR 0008, ADR 0010, ADR 0012, ADR 0019
**Source of Truth:** `schemas/worker.yaml`, `schemas/task.yaml`, `VIKUNJA.md`

---

## 1. Architecture & Concurrency Flow

```
                     PostgreSQL (Durable Tasks & Workers)
                                       │
                                       ▼
                       WorkloadScheduler.find_candidate()
                   [SQL Capacity Gate: active < max_concurrent]
                                       │
                    ┌──────────────────┴──────────────────┐
                    │                                     │
          Worker Saturated                     Capacity Available
          (active == max)                      (active < max)
                    │                                     │
                    ▼                                     ▼
               Return None                       Return Runnable Task
                    │                                     │
                    │                                     ▼
                    │                          WorkerOrchestrator Pool
                    │                        ┌─────────────────────────┐
                    │                        │ Slot 1: Task A (async)  │
                    │                        │ Slot 2: Task B (async)  │
                    │                        │ Slot N: Task ...        │
                    │                        └────────────┬────────────┘
                    │                                     │
                    │                                     ▼
                    │                         Independent Session & Leases
                    │                        ┌─────────────────────────┐
                    │                        │ Session A ── Heartbeat A│
                    │                        │ Session B ── Heartbeat B│
                    │                        └─────────────────────────┘
                    │                                     │
                    ▼                                     ▼
             Sleep Poll Interval                   Adapter Execution
```

---

## 2. Component Specifications

### 2.1 `WorkloadScheduler` Capacity Gating
- **File:** `src/personaos/workload/scheduler.py`
- In `_CANDIDATE_SELECTION_SQL`:
  Add predicate checking active tasks:
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
- If the worker currently has `count >= max_concurrent_tasks`, query evaluates to empty, returning `None`.

### 2.2 `WorkerOrchestrator` Concurrency Pool
- **File:** `src/personaos/orchestrator/worker.py`
- **Fields:**
  - `session_factory: async_sessionmaker[AsyncSession] | None = None`
  - `max_concurrency: int | None = None`
  - `_active_tasks: set[asyncio.Task[Any]] = field(default_factory=set)`
- **Behavior:**
  - In `run()`:
    - Prune completed tasks from `_active_tasks`.
    - If `max_concurrency` is set and `len(_active_tasks) >= max_concurrency`, wait for at least one task to complete (`asyncio.wait(return_when=FIRST_COMPLETED)`).
    - If capacity available, call `process_one_concurrent()` or dispatch.
  - In `stop()`:
    - Set stop requested flag.
    - Await completion of all active tasks in `_active_tasks`.

---

## 3. Acceptance Test Matrix

- **CAP-01:** `test_scheduler_capacity_gate_blocks_saturated_worker`: Saturated worker with active task count == max_concurrent_tasks receives no candidate tasks.
- **CAP-02:** `test_scheduler_capacity_gate_allows_available_slots`: Worker with active < max_concurrent_tasks receives candidates up to limit.
- **CAP-03:** `test_concurrent_task_execution_in_runner`: Runner executes multiple tasks simultaneously when max_concurrent_tasks > 1.
- **CAP-04:** `test_concurrent_task_heartbeat_isolation`: Sibling concurrent tasks maintain isolated heartbeats; delay in one task does not impact sibling heartbeats.
- **CAP-05:** `test_graceful_shutdown_drains_concurrent_pool`: Stop request allows all concurrent in-flight tasks to finish cleanly.
