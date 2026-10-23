# ADR 0021: Task Checkpoint Resumption and Resilient Restart Recovery

**Status:** Proposed (Step 18 Design Phase)
**Date:** 2026-10-23
**Deciders:** PersonaOS Architecture Team
**Consulted:** Workload Execution, Reliability, Runtime Adapters, Security
**Informed:** Operators, Control Plane

---

## Context & Problem Statement

PersonaOS is founded on the architectural principle: **"Compute is replaceable; the worker is persistent."**
In `VIKUNJA.md`, the primary v0.1 success criterion and Phase-1 acceptance test (Liang, 2026-10-07) define the exact property PersonaOS exists to deliver:
> 1. Worker is bound to a Node; adapter starts execution.
> 2. Task emits `task.checkpointed`; intermediate state is saved to `task_checkpoints`.
> 3. KILL / STOP Node mid-task (simulating process crash, ungraceful kill, or host loss).
> 4. START a different Node (same `worker_id`, different runtime host).
> 5. LOAD the same worker identity and task checkpoint (`tasks.checkpoint_ref` → `task_checkpoints`).
> 6. **RESUME the task from the last checkpoint** — no manual intervention, no lost work, and no duplicated side effects.

### The Verified Gap in Current Code

In previous milestones (Steps 12a and 13b), crash resilience and lease fencing were implemented (`ADR 0016`). However, to guard against undefined partial side effects before checkpoint resume contracts were defined, `ADR 0016` chose an interim fail-safe: **clear `checkpoint_ref = NULL` and restart recovery from scratch (`checkpoint=None`)**.

This interim behavior is codified in production code and test suites:
1. **`WorkerExecutionKernel.reclaim` ([`src/personaos/execution/kernel.py`](file:///root/orca/workspaces/PersonaOS/yabby/src/personaos/execution/kernel.py#L623-L656)):
   ```python
   reclaim_sql = text("""
       UPDATE tasks
       SET status = 'in_progress',
           assigned_worker = :worker_id,
           lock_expires_at = NOW() + interval '{ttl_seconds} seconds',
           generation = generation + 1,
           checkpoint_ref = NULL,
           started_at = NOW(),
           retry_count = retry_count + 1
       WHERE id = :task_id ...
   """)
   ...
   # Recovery execution starts from scratch with checkpoint=None
   if self._adapter is not None:
       adapter_res = await self._adapter.execute(context=context, checkpoint=None)
   ```
2. **E2E Crash Recovery Tests ([`tests/e2e/test_process_crash_recovery.py`](file:///root/orca/workspaces/PersonaOS/yabby/tests/e2e/test_process_crash_recovery.py#L233-L234)):**
   ```python
   # Checkpoint is None because ADR 0016 AC #8 clears checkpoint_ref on reclaim
   assert checkpoint is None, "ADR 0016 AC #8 mandates recovery starts with checkpoint=None"
   ```
3. **`OpenClawAdapter.execute` ([`src/personaos/execution/adapters/openclaw.py`](file:///root/orca/workspaces/PersonaOS/yabby/src/personaos/execution/adapters/openclaw.py#L540-L546)):**
   The `checkpoint: Any | None = None` parameter is accepted by the method signature but completely ignored by `_OpenClawProcessRunner`.
4. **`TaskReviewService.approve` ([`src/personaos/workload/review.py`](file:///root/orca/workspaces/PersonaOS/yabby/src/personaos/workload/review.py#L128)):**
   `task_row.checkpoint_ref = None` unconditionally erases checkpoint history when re-queueing a task for execution.

Because recovery wipes `checkpoint_ref` and runs with `checkpoint=None`, any work completed prior to a crash is permanently lost, and the replacement node must repeat all previous steps from the beginning. This directly violates the v0.1 success criterion and leaves the Phase-1 killer test incomplete.

---

## Decision

We will implement **Task Checkpoint Resumption and Resilient Restart Recovery** across the kernel, workload review service, and runtime adapters.

### 1. Checkpoint Preservation and Loading on Kernel Reclaim
- In `WorkerExecutionKernel.reclaim()`:
  - Do **not** set `checkpoint_ref = NULL` during atomic reclaim of `IDEMPOTENT` tasks.
  - Preserve the existing `checkpoint_ref` pointer on the task record.
  - Increment `generation = generation + 1` to fence any writes from the previous worker.
  - If `task.checkpoint_ref` is present:
    - Atomically load checkpoint state via `checkpoint_load(session, task.checkpoint_ref)`.
    - Pass `checkpoint=checkpoint_state` into `adapter.execute(context=context, checkpoint=checkpoint_state)`.
    - Emit audit event `task.resumed` or include `checkpoint_ref` in recovery metrics.
  - If `task.checkpoint_ref` is `NULL`:
    - Gracefully fall back to fresh execution with `checkpoint=None`.

### 2. Intermediate Fenced Checkpointing Protocol
- Adapters executing multi-step tasks can persist progress at discrete, idempotent boundaries:
  - Adapter invokes `kernel.checkpoint_write(task, ref, state)` or returns `Result(status="in_progress", new_checkpoint=state)`.
  - `WorkerExecutionKernel.checkpoint_write()` enforces generation fencing:
    ```sql
    INSERT INTO task_checkpoints (ref, task_id, state) VALUES (:ref, :task_id, :state);
    UPDATE tasks
    SET checkpoint_ref = :ref
    WHERE id = :task_id
      AND generation = :generation
      AND status = 'in_progress';
    ```
  - Emits `task.checkpointed` event with `task_id`, `checkpoint_ref`, and `actor_id`.
  - If the worker was reclaimed concurrently, the fenced update fails with `StaleWorkerError`, preventing zombie writes from corrupting the new owner's state.

### 3. OpenClaw Runtime Adapter Checkpoint Ingestion
- In `OpenClawAdapter`:
  - When `checkpoint` is passed to `execute(context, checkpoint)`:
    - `_OpenClawProcessRunner` inspects `checkpoint` data.
    - If `checkpoint` contains state (e.g. `step`, `completed_manifests`, `session_history`), the initial prompt to OpenClaw is augmented with structured resumption instructions:
      ```
      [RESUMPTION CONTEXT]
      This task is resuming from checkpoint ref: {checkpoint_ref}
      Completed stages: {completed_stages}
      Current state snapshot: {state}
      DO NOT repeat completed stages. Resume directly from: {next_stage}.
      ```
    - Enables OpenClaw to continue complex multi-step workflows across process restarts without repeating side effects.

### 4. Operator Review Re-entry with Checkpoint Control
- In `TaskReviewService.approve()`:
  - Add parameter `resume_from_checkpoint: bool = True` and `checkpoint_ref_override: str | None = None`.
  - If `resume_from_checkpoint=True` (default):
    - Preserve `task_row.checkpoint_ref` so the re-queued task resumes from its intermediate progress.
  - If `checkpoint_ref_override` is provided by the operator:
    - Validate that the referenced checkpoint exists and belongs to this task, then bind `task_row.checkpoint_ref = checkpoint_ref_override`.
  - If `resume_from_checkpoint=False`:
    - Reset `task_row.checkpoint_ref = NULL` to enforce a clean-slate restart.

### 5. Zero Database Migrations Required
- The existing six-table schema (ADR 0008, Migration 0001) already includes:
  - `tasks.checkpoint_ref VARCHAR(255) NULL REFERENCES task_checkpoints(ref)`
  - `task_checkpoints (ref VARCHAR(255) PRIMARY KEY, task_id VARCHAR(255) REFERENCES tasks(id), state JSONB, created_at TIMESTAMPTZ)`
  - Event type `task.checkpointed` is already in the `task_events.type` constraint.
- No DDL migrations or schema alterations are needed.

---

## Preserved Invariants

1. **Kernel Claim and Fencing Authority:** `WorkerExecutionKernel` remains the sole authority for claims, generation increments, lease checks, and completion.
2. **Side-Effect Safety Guard:** Non-idempotent tasks (`side_effect_classification != 'IDEMPOTENT'`) remain blocked from automatic reclaim; they strictly route to `status = 'review'` (`exhaustion_reason = 'side-effect safety: auto-reclaim blocked'`).
3. **Six-Table Monolith Scope:** No new tables, Redis caches, or external message brokers.
4. **Adapter Independence:** OpenClaw remains an external process; communication occurs strictly over stdio ACP JSON-RPC.

---

## Alternatives Considered

| Alternative | Pros | Cons | Decision |
| :--- | :--- | :--- | :--- |
| **A. Keep `checkpoint_ref = NULL` on recovery (Status Quo)** | Zero risk of stale checkpoint corruption. | Completely discards intermediate work; fails the Phase-1 acceptance test. | **Rejected** |
| **B. Resumption via External Distributed Snapshot Store (e.g. S3/MinIO)** | Decouples snapshot payloads from PostgreSQL. | Introduces heavy external infrastructure dependencies violating ADR 0001 and ADR 0008. | **Rejected** |
| **C. Fenced In-Kernel Checkpoint Resumption (Proposed)** | Reuses established tables (`tasks`, `task_checkpoints`); generation-fenced; delivers the Phase-1 killer test. | Requires adapters to structure resumption context cleanly. | **Accepted** |

---

## Non-Goals

- Distributed multi-node consensus or live process migration across hypervisors.
- Time-travel debugging or branching execution DAGs from arbitrary historical checkpoints (deferred to Phase 4 Replay Engine).
- Automated rollback of non-idempotent external side effects (handled by operator review and idempotent task declarations).
