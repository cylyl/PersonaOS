# ADR 0018: Task Review Resolution & Operator Re-Entry Lifecycle

**Status:** Accepted (Phase 2 Overnight Engineering Run)
**Date:** 2026-10-22
**Deciders:** PersonaOS Architecture Team
**Consulted:** Control Plane, Workload Execution, Reliability, Security
**Informed:** Operators, Runtime Adapters

---

## Context & Problem Statement

PersonaOS guarantees that non-idempotent tasks (`side_effect_classification` = `UNKNOWN` or `HAS_SIDE_EFFECTS`) are never automatically retried or silently dropped into terminal `failed` status when ambiguous failures occur:
1. **Ambiguous Adapter Failures (ADR 0017 / Step 14b)**: If an in-flight prompt experiences a subprocess crash, execution timeout, or unexpected cancellation after transmission to OpenClaw (or any runtime adapter), `WorkerExecutionKernel` safely isolates the task into `status = 'review'` with `exhaustion_reason = 'ambiguous_in_flight_failure'`.
2. **Orphan & Lease Expiry Recovery (ADR 0016 / Step 13b)**: If an execution worker node crashes or hangs and its lease expires (`lock_expires_at < NOW()`), non-idempotent tasks are routed to `status = 'review'` rather than auto-reclaimed.
3. **Retry Exhaustion (ADR 0016)**: Non-idempotent tasks reaching `retry_count >= max_retries` route to `status = 'review'` with `exhaustion_reason = 'retry_limit_exhausted'`.

While tasks are safely quarantined in the `'review'` state, the control plane previously lacked a complete, reliable, and auditable operational lifecycle to resolve them. Specifically:

1. **Destructive Worker-Affinity Reset on Approval**:
   The prior implementation of `approve_task` set `assigned_worker = None`. Clearing worker assignment violates the invariant in `ProfileRegistry.resolve_execution_context`, which mandates that `task.assigned_worker` must be present to load `<profiles_dir>/<assigned_worker>/v<profile_version>.yaml`. Re-executing a task with `assigned_worker = None` raises `ProfileError`.
2. **Missing Manual Completion for Out-of-Band Success**:
   If an ambiguous task actually completed its external side effect (e.g. provisioned a cloud resource) before the adapter crashed or timed out, the operator can verify this out-of-band. The API previously only offered `approve` (which re-queues and re-runs the task, causing **duplicate external side effects**) or `reject` (which falsely marks it `failed`).
3. **Destruction of Execution Diagnostics**:
   When an ambiguous failure routes to `review`, `tasks.result` stores forensic evidence (`{"ambiguous": true, "session_id": ..., "accumulated_text": ...}`). Overwriting `tasks.result` blindly during operator resolution destroys this forensic evidence.
4. **Audit Event Schema Integrity**:
   `task_events` has a PostgreSQL CHECK constraint:
   `CHECK (type IN ('task.created', 'task.claimed', 'task.checkpointed', 'task.completed', 'task.failed'))`.
   Emitting `task.created` for re-queuing existing tasks is semantically inaccurate and corrupts task creation metrics.

---

## Decision

Implement the **Task Review Resolution & Operator Re-Entry Lifecycle** via explicit, generation-fenced, authenticated, and evidence-preserving control-plane operations.

### 1. Scope Exclusivity: Strictly `status = 'review'`
The review lifecycle applies exclusively to tasks where `status = 'review'`. The dormant placeholder state `'blocked'` is excluded from this milestone's API contract. Requests targeting any task not in `'review'` return `HTTP 409 Conflict`.

### 2. Three Deterministic Resolution Paths

```
                             ┌──────────────────────────┐
                             │     status='review'      │
                             └────────────┬─────────────┘
                                          │
            ┌─────────────────────────────┼─────────────────────────────┐
            ▼                             ▼                             ▼
     POST /approve                 POST /resolve                 POST /reject
   (Safe Re-enqueue)             (Manual Complete)            (Terminal Fail)
            │                             │                             │
            ▼                             ▼                             ▼
     status='queued'              status='completed'            status='failed'
     gen = gen + 1                gen = gen + 1                 gen = gen + 1
     worker preserved             completed_at set              completed_at set
     lease cleared                lease cleared                 lease cleared
     exhaustion cleared           exhaustion cleared            exhaustion='operator_rejected'
     prior diagnostics archived   diagnostics preserved         last_error recorded
     checkpoint cleared           task.completed emitted        task.failed emitted
     task.requeued emitted
```

#### Path A: Safe Re-enqueue (`POST /tasks/{task_id}/approve`)
Atomically transitions a task from `review` to `queued`:
- **Preserves Worker Affinity**: Retains original `assigned_worker` by default. If `reassign_worker_id` is supplied:
  - Validates that the target worker exists in `workers` (HTTP 404 if missing).
  - Validates that the target worker is active/eligible (not retired or quarantined).
  - Atomically re-snapshots `profile_version = target_worker.current_profile_version` to guarantee valid profile loading.
- **Generation Fencing & Lease Clearing**: Increments `generation = generation + 1` and sets `lock_expires_at = NULL`.
- **Checkpoint Reset**: Clears `checkpoint_ref = NULL` so execution starts fresh from scratch per ADR 0016.
- **Guarded Classification & Retry Reset**:
  - Resets `retry_count = 0` when `reset_retries: bool = True`.
  - If `reset_retries: bool = False` and `retry_count >= max_retries`, rejects with HTTP 422 unless `max_retries` is explicitly increased.
  - Updating `side_effect_classification` to `IDEMPOTENT` requires an explicit, audited `classification_attestation` (min 15 characters).
- **Evidence Archival**: Clears `tasks.result` for the fresh run, but archives prior diagnostic output and prior error into the audit event payload.
- **Audit Event**: Emits `task.requeued`.

#### Path B: Manual Completion (`POST /tasks/{task_id}/resolve`)
Atomically transitions a task from `review` to `completed` without re-execution:
- **Prevents Duplicate Side Effects**: Allows the operator to attest to out-of-band external completion.
- **Evidence Preservation**: Constructs a composite `tasks.result` containing:
  - `verified_output`: verified operator output dictionary.
  - `resolution_metadata`: actor ID, timestamp, and justification.
  - `prior_execution`: archived diagnostics, partial streaming text, session ID, prior error, and prior checkpoint ref.
- **Generation Fencing & Lease Clearing**: Increments `generation = generation + 1`, sets `lock_expires_at = NULL`, and sets `completed_at = func.now()`.
- **Clears Exhaustion**: Sets `exhaustion_reason = NULL`.
- **Audit Event**: Emits `task.completed` with `payload={"action": "operator_resolved", ...}`.

#### Path C: Terminal Rejection (`POST /tasks/{task_id}/reject`)
Atomically transitions a task from `review` to `failed`:
- **Terminal Timestamp**: Sets `completed_at = func.now()`.
- **Generation Fencing & Lease Clearing**: Increments `generation = generation + 1` and sets `lock_expires_at = NULL`.
- **Records Reason**: Sets `last_error = reason` and `exhaustion_reason = 'operator_rejected'`. Prior execution diagnostics in `tasks.result` are retained.
- **Audit Event**: Emits `task.failed` with `payload={"action": "operator_rejected", ...}`.

---

### 3. Optimistic Concurrency & Generation Fencing

All resolution mutations require `expected_generation: int`.
The conditional database update validates within a single transaction:
```sql
SELECT * FROM tasks WHERE id = :task_id FOR UPDATE;
```
If `task.status != 'review'` or `task.generation != expected_generation`, the transaction aborts and returns `HTTP 409 Conflict`.
A successful operation increments generation exactly once.

---

### 4. Migration 0005 (`0005_task_review_events.py`)

To preserve event sourcing integrity and avoid corrupting `task.created` counts, Migration 0005 updates the `task_events_type_check` constraint:
```sql
ALTER TABLE task_events DROP CONSTRAINT task_events_type_check;
ALTER TABLE task_events ADD CONSTRAINT task_events_type_check
  CHECK (type IN ('task.created', 'task.claimed', 'task.checkpointed', 'task.completed', 'task.failed', 'task.requeued'));
```
Downgrade preserves historical event records by mapping `task.requeued` back to `task.created` with `payload["original_type"] = "task.requeued"` before restoring the 5-element constraint.

---

### 5. Operator Identity & Authentication

- Request authorization is evaluated by `AuthenticatedOperatorDep`.
- When `settings.operator_api_token` is configured, requests must supply `Authorization: Bearer <token>`. Missing or invalid tokens return `HTTP 401 Unauthorized`.
- When token authentication is disabled (local testing/dev mode with no token configured), identity defaults to `system:operator` or cooperative `X-Operator-Id` header.
- Client-supplied `actor_id` in JSON request bodies is never trusted as proof of identity.

---

### 6. Architectural Invariants Preserved

1. **Kernel Supremacy**: `WorkerExecutionKernel` remains the sole owner of runtime execution and atomic task claims (`queued -> in_progress`).
2. **Workload Read-Only FIFO**: `WorkloadService.next_runnable()` remains strictly read-only and orders by `created_at ASC, id ASC`.
3. **No External Queues**: PostgreSQL remains the sole durable state store; no Redis or distributed brokers.
4. **No Automatic Replay**: Non-idempotent tasks never auto-replay after ambiguous failure.
5. **Atomic State & Event**: State change and audit event are committed in the same database transaction; failure rolls back both.

---

## Consequences

### Positive
- Prevents runtime crashes on task re-entry by preserving `assigned_worker` and profile snapshots.
- Prevents duplicate side effects by enabling verified manual completion.
- Preserves forensic diagnostics in `tasks.result["prior_execution"]`.
- Enforces generation fencing against concurrent operator races and stale worker writes.
- Introduces `task.requeued` for pristine audit trail tracking.

### Negative / Trade-offs
- Migration 0005 updates table CHECK constraints.
- Storing composite results slightly increases JSON payload size.
