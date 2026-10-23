# Specification: Task Checkpoint Resumption & Resilient Restart Recovery

**Module:** `personaos.execution`, `personaos.workload`, `personaos.execution.adapters`
**Related ADR:** ADR 0021, ADR 0010, ADR 0016, ADR 0017, ADR 0018
**Source of Truth:** `schemas/task.yaml`, `schemas/event.yaml`, `VIKUNJA.md`

---

## 1. Architecture & Execution Flow

```
                      Node-1 (Failing Host)                         Node-2 (Recovery Host)
                   ┌────────────────────────┐                    ┌────────────────────────┐
                   │ WorkerRunner           │                    │ WorkerRunner           │
                   │ (worker-01 @ node-1)   │                    │ (worker-01 @ node-2)   │
                   └───────────┬────────────┘                    └───────────┬────────────┘
                               │                                             │
                       1. Claims Task (Gen 1)                                │
                               │                                             │
                       2. Writes Checkpoint                                  │
                          (step: "applied_k8s")                              │
                               │                                             │
                      💥 3. NODE CRASHES / KILLED                           │
                         (Lease expires in DB)                               │
                                                                             │
                                                                     4. Discovers Stale Task
                                                                        (lock_expires_at < NOW())
                                                                             │
                                                                     5. kernel.reclaim()
                                                                        - Preserves checkpoint_ref
                                                                        - Increments Gen (Gen 2)
                                                                        - Loads Checkpoint State
                                                                             │
                                                                     6. adapter.execute(
                                                                          context,
                                                                          checkpoint=state
                                                                        )
                                                                        - Reads step: "applied_k8s"
                                                                        - Skips step 1
                                                                        - Executes step 2
                                                                             │
                                                                     7. kernel.complete()
                                                                        (Gen 2 verified)
```

---

## 2. Component Contracts & Interfaces

### 2.1 `WorkerExecutionKernel.reclaim` Contract
- **File:** `src/personaos/execution/kernel.py`
- **Method Signature:**
  ```python
  async def reclaim(self, task: Task) -> None:
      """Atomically reclaims an expired in-progress task, loads checkpoint, and resumes execution."""
  ```
- **Behavior:**
  1. Verify worker status is eligible (`status NOT IN ('quarantined', 'retired')`).
  2. Execute atomic SQL update:
     ```sql
     UPDATE tasks
     SET status = 'in_progress',
         assigned_worker = :worker_id,
         assigned_node = :node_id,
         lock_expires_at = NOW() + interval ':ttl_seconds seconds',
         generation = generation + 1,
         started_at = NOW(),
         retry_count = retry_count + 1
     WHERE id = :task_id
       AND status = 'in_progress'
       AND generation = :observed_gen
       AND lock_expires_at < NOW()
       AND side_effect_classification = 'IDEMPOTENT'
       AND retry_count < max_retries
     RETURNING id, generation, checkpoint_ref;
     ```
  3. Notice `checkpoint_ref` is **preserved** (not overwritten with `NULL`).
  4. If `claimed_row.checkpoint_ref` is not `None`:
     - Load checkpoint state from `task_checkpoints` via `checkpoint_load(self._session, checkpoint_ref)`.
     - Context resolution resolves the effective worker execution context.
     - Execute `await self._adapter.execute(context=context, checkpoint=checkpoint_state)`.
  5. If `claimed_row.checkpoint_ref` is `None`:
     - Execute `await self._adapter.execute(context=context, checkpoint=None)`.
  6. Upon completion, save result and commit transaction under the new generation.

### 2.2 `WorkerExecutionKernel.checkpoint_write` Contract
- **File:** `src/personaos/execution/kernel.py`
- **Method Signature:**
  ```python
  async def checkpoint_write(self, task: Task, ref: str, state: dict[str, Any]) -> None:
      """Persists checkpoint state and links it to task with generation fencing."""
  ```
- **Behavior:**
  1. Insert row into `task_checkpoints (ref, task_id, state)`.
  2. Update `tasks SET checkpoint_ref = :ref WHERE id = :task_id AND generation = :current_generation AND status = 'in_progress'`.
  3. If `rowcount == 0`: Raise `StaleWorkerError("task reclaimed; checkpoint write rejected")`.
  4. Emit audit event `task.checkpointed`.

### 2.3 `OpenClawAdapter` Resumption Contract
- **File:** `src/personaos/execution/adapters/openclaw.py`
- **Behavior:**
  1. Pass `checkpoint` into `_OpenClawProcessRunner`.
  2. If `checkpoint` is present and contains non-empty dictionary:
     - Prepend `[TASK RESUMPTION STATE]` banner into the prompt payload submitted to `session/prompt`.
     - Explicitly instruct the model/agent to resume from the checkpointed offset and skip completed stages.
  3. When `OpenClaw` outputs intermediate step markers, parse and return `Result(status="in_progress", new_checkpoint=state)`.

### 2.4 `TaskReviewService.approve` Resumption Contract
- **File:** `src/personaos/workload/review.py`
- **Signature:**
  ```python
  async def approve(
      self,
      task_id: str,
      expected_generation: int,
      operator_id: str,
      reason: str,
      reassign_worker_id: str | None = None,
      reset_retries: bool = True,
      max_retries: int | None = None,
      attestation: dict[str, Any] | None = None,
      resume_from_checkpoint: bool = True,
      checkpoint_ref_override: str | None = None,
  ) -> Task:
  ```
- **Behavior:**
  1. If `checkpoint_ref_override` is specified:
     - Verify checkpoint exists in `task_checkpoints` for `task_id`.
     - Set `task_row.checkpoint_ref = checkpoint_ref_override`.
  2. Else if `resume_from_checkpoint` is `True`:
     - Retain `task_row.checkpoint_ref` as-is.
  3. Else:
     - Set `task_row.checkpoint_ref = NULL`.

---

## 3. Concurrency & Threat Analysis

| Threat / Race Condition | Risk | Mitigation |
| :--- | :--- | :--- |
| **Zombie Worker Checkpoint Overwrite** | A slow/partitioned worker resumes after lease expiry and attempts to write an old checkpoint over the new worker's checkpoint. | Generation fencing in `checkpoint_write`: `UPDATE tasks WHERE generation = :current_generation` fails (`rowcount = 0`), raising `StaleWorkerError`. |
| **Corrupted Checkpoint State** | Stale or unparseable JSON in `task_checkpoints` causes crash on resume. | `checkpoint_load` validates that `state` is a valid dictionary; on validation failure, logs warning, falls back to `checkpoint=None`, and routes task to review if retries exhausted. |
| **Non-Idempotent Duplicate Action** | Resuming task executes an external API call that was already executed prior to crash. | Non-idempotent tasks are strictly blocked from auto-reclaim in `WorkloadScheduler` and `WorkerOrchestrator` (`side_effect_classification == 'IDEMPOTENT'` requirement). |
| **Tampered Checkpoint Reference** | Operator specifies an invalid or cross-tenant checkpoint reference via `checkpoint_ref_override`. | SQL check validates `task_checkpoints.task_id == task_row.id`; rejects mismatched refs with `InvalidActionError`. |

---

## 4. Test Matrix & Acceptance Scenarios

| Test ID | Test Name | Suite | Description |
| :--- | :--- | :--- | :--- |
| **CP-01** | `test_reclaim_loads_and_passes_checkpoint_to_adapter` | Integration | Verifies `reclaim()` preserves `checkpoint_ref`, loads state from DB, and passes it to `adapter.execute(..., checkpoint=state)`. |
| **CP-02** | `test_reclaim_with_null_checkpoint_starts_clean` | Integration | Verifies task with `checkpoint_ref = NULL` reclaims cleanly with `checkpoint=None`. |
| **CP-03** | `test_fenced_checkpoint_write_blocks_stale_worker` | Integration | Verifies worker with generation `N` cannot write checkpoint after reclaim incremented generation to `N+1`. |
| **CP-04** | `test_openclaw_adapter_ingests_checkpoint_into_prompt` | Unit | Verifies `_OpenClawProcessRunner` injects resumption context into the prompt payload sent to `session/prompt`. |
| **CP-05** | `test_review_approve_preserves_checkpoint_by_default` | Integration | Verifies `TaskReviewService.approve(..., resume_from_checkpoint=True)` leaves `checkpoint_ref` intact. |
| **CP-06** | `test_review_approve_resets_checkpoint_when_requested` | Integration | Verifies `TaskReviewService.approve(..., resume_from_checkpoint=False)` sets `checkpoint_ref = NULL`. |
| **CP-07** | `test_review_approve_with_checkpoint_ref_override` | Integration | Verifies operator can bind task to a specific valid prior checkpoint. |
| **CP-08** | `test_e2e_real_process_kill_resumes_from_checkpoint` | E2E | Phase-1 killer test: Subprocess Node-1 writes checkpoint, gets SIGKILLed, Node-2 recovers and resumes from checkpoint without repeating Step 1. |

---

## 5. Ordered Implementation Plan

1. **Phase 1: Kernel Checkpoint Reclaim & Resumption**
   - Update `WorkerExecutionKernel.reclaim()` in `src/personaos/execution/kernel.py` to preserve `checkpoint_ref` and load state via `checkpoint_load()`.
   - Add unit/integration tests in `tests/integration/test_worker_execution_kernel.py`.
2. **Phase 2: Operator Review Service Checkpoint Controls**
   - Update `TaskReviewService.approve()` in `src/personaos/workload/review.py` to support `resume_from_checkpoint` and `checkpoint_ref_override`.
   - Update task API routes and schemas in `src/personaos/api/routes/tasks.py`.
   - Add integration tests in `tests/integration/test_task_review_lifecycle.py`.
3. **Phase 3: Runtime Adapter Checkpoint Ingestion**
   - Update `OpenClawAdapter` and `_OpenClawProcessRunner` in `src/personaos/execution/adapters/openclaw.py` to format resumption context into prompt requests.
   - Add unit tests in `tests/unit/execution/test_openclaw_adapter.py`.
4. **Phase 4: End-to-End Resilience Verification**
   - Update `tests/e2e/test_resilience_recovery.py` and `tests/e2e/test_process_crash_recovery.py` to assert that Node-2 actually receives the intermediate checkpoint and skips completed steps.
   - Verify full test suite and quality gates (Ruff, Mypy, Git check).
