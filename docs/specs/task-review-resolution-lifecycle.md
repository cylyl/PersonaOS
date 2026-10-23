# Task Review Resolution & Operator Re-Entry Lifecycle — Specification

**Spec status:** Accepted (Phase 2 Overnight Engineering Run)
**Parent ADR:** ADR 0018
**Scope:** Control-plane quarantine resolution, worker affinity preservation, forensic evidence retention, and atomic operator actions.

---

## 1. Scope & Architectural Invariants

When non-idempotent tasks encounter in-flight adapter crashes, timeouts, lease expirations, or retry exhaustion, PersonaOS quarantines them into `status = 'review'`.

This specification formalizes the operational and control-plane lifecycle required to safely inspect and resolve tasks in `review`, preventing duplicate side-effect execution and preserving worker-profile snapshot consistency.

### Architectural Invariants
1. **Target Status Exclusivity**:
   - The review lifecycle applies strictly to tasks where `status = 'review'`. Requests targeting any task not in `'review'` return `HTTP 409 Conflict`.
2. **Worker Affinity & Profile Snapshot Consistency**:
   - `tasks.profile_version` is immutable for a given assigned worker.
   - Re-queuing a task preserves its original `assigned_worker`.
   - If `reassign_worker_id` is supplied, the target worker must exist in `workers`, and `tasks.profile_version` must be re-snapshotted from `target_worker.current_profile_version` to prevent profile resolution errors.
3. **Forensic Evidence Preservation**:
   - Original adapter failure diagnostics (session ID, partial stdout, and error) in `tasks.result` must never be erased.
   - Manual completion nests prior diagnostics under `tasks.result["prior_execution"]`.
   - Re-enqueue archives prior diagnostics into the emitted `task.requeued` audit event payload.
4. **Atomic Generation Fencing & Optimistic Concurrency**:
   - Every resolution action increments `generation = generation + 1` and clears `lock_expires_at = NULL`.
   - All resolution mutation requests require `expected_generation: int`. Mismatches return `HTTP 409 Conflict`.
5. **Authenticated Identity**:
   - Operator identity is derived strictly from verified credentials (`AuthenticatedOperatorDep`), never from client-supplied JSON request body fields.
6. **Transaction Consistency**:
   - State transition, evidence update, and event emission occur in the same database transaction under `SELECT ... FOR UPDATE`.
7. **Read-Only Workload FIFO**:
   - `WorkloadService.next_runnable()` remains strictly read-only and orders by `created_at ASC, id ASC`.

---

## 2. API Data Contracts & Validation Rules

All request schemas strictly enforce `extra = "forbid"` to reject unknown request fields.

### 2.1 Request Schemas (`src/personaos/api/schemas.py`)

```python
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class ClassificationAttestation(BaseModel):
    """Accountable operator declaration for downgrading task classification to IDEMPOTENT."""
    model_config = ConfigDict(extra="forbid")

    requested_classification: Literal["IDEMPOTENT"] = "IDEMPOTENT"
    safety_justification: str = Field(
        ...,
        min_length=15,
        max_length=1000,
        description="Detailed operator attestation justifying why automatic replay is safe",
    )
    verification_reference: str | None = Field(
        None, max_length=200, description="Ticket, commit, or run reference confirming safety"
    )


class TaskApproveRequest(BaseModel):
    """Payload for approving and re-queuing a task from review state."""
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        ...,
        min_length=10,
        max_length=1000,
        description="Operational justification for approval and re-queue",
    )
    expected_generation: int = Field(
        ..., ge=0, description="Mandatory generation fence to prevent concurrent lost updates"
    )
    reassign_worker_id: str | None = Field(
        None, description="Explicitly reassign task to a different valid worker ID"
    )
    reset_retries: bool = Field(
        True, description="Reset retry_count to 0 to provide a fresh retry budget"
    )
    max_retries: int | None = Field(
        None, ge=1, le=20, description="Optionally increase max_retries limit"
    )
    attestation: ClassificationAttestation | None = Field(
        None, description="Mandatory attestation if changing classification to IDEMPOTENT"
    )


class TaskResolveRequest(BaseModel):
    """Payload for manually marking a reviewed task completed with verified output."""
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        ...,
        min_length=10,
        max_length=1000,
        description="Verification notes explaining out-of-band success",
    )
    expected_generation: int = Field(
        ..., ge=0, description="Mandatory generation fence to prevent concurrent lost updates"
    )
    result: dict[str, Any] = Field(
        default_factory=dict, description="Verified output dictionary attested by the operator"
    )


class TaskRejectRequest(BaseModel):
    """Payload for terminally failing a reviewed task."""
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        ...,
        min_length=10,
        max_length=1000,
        description="Rejection reason / failure description",
    )
    expected_generation: int = Field(
        ..., ge=0, description="Mandatory generation fence to prevent concurrent lost updates"
    )
```

---

### 2.2 Response Schemas

```python
class TaskReviewContextResponse(BaseModel):
    """Aggregated diagnostic view for operator decision making."""
    model_config = ConfigDict(from_attributes=True)

    task: TaskResponse
    assigned_worker_id: str | None
    worker_name: str | None
    worker_role: str | None
    worker_runtime: str | None
    profile_version_snapshot: int
    worker_current_profile_version: int | None
    is_snapshot_outdated: bool
    execution_evidence: dict[str, Any]
    recent_events: list[TaskEventResponse]
    checkpoints_count: int
    latest_checkpoint: TaskCheckpointResponse | None
    checkpoint_history: list[TaskCheckpointResponse]
```

---

## 3. Migration 0005 (`0005_task_review_events.py`)

Alters the `task_events_type_check` constraint on `task_events` to include `'task.requeued'`.

```python
"""add task.requeued to task_events check constraint (Step 15 / ADR 0018)

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-22
"""

from alembic import op

def upgrade() -> None:
    op.drop_constraint("task_events_type_check", "task_events", type_="check")
    op.create_check_constraint(
        "task_events_type_check",
        "task_events",
        "type IN ('task.created', 'task.claimed', 'task.checkpointed', "
        "'task.completed', 'task.failed', 'task.requeued')",
    )

def downgrade() -> None:
    # Preserve historical records before restoring 5-element constraint
    op.execute(
        "UPDATE task_events "
        "SET type = 'task.created', "
        "    payload = jsonb_set(payload, '{original_type}', '\"task.requeued\"'::jsonb) "
        "WHERE type = 'task.requeued'"
    )
    op.drop_constraint("task_events_type_check", "task_events", type_="check")
    op.create_check_constraint(
        "task_events_type_check",
        "task_events",
        "type IN ('task.created', 'task.claimed', 'task.checkpointed', "
        "'task.completed', 'task.failed')",
    )
```

---

## 4. Acceptance Test Plan Matrix

| # | Test Scenario | Preconditions / Setup | Operation | Expected Outcome |
|---|---|---|---|---|
| **1** | **Worker Affinity Preserved** | Task in `review` with `assigned_worker='worker-a'`, `profile_version=2` | `POST /tasks/{id}/approve` | Status `queued`, `assigned_worker='worker-a'`, `profile_version=2`, generation increments, lease cleared |
| **2** | **Explicit Reassignment** | Task in `review`. `worker-b` exists with `current_profile_version=3` | `POST /tasks/{id}/approve` with `reassign_worker_id='worker-b'` | Status `queued`, `assigned_worker='worker-b'`, `profile_version=3` |
| **3** | **Reassignment 404** | Task in `review` | `POST /tasks/{id}/approve` with `reassign_worker_id='missing'` | Returns `HTTP 404`, task unchanged |
| **4** | **Manual Resolution Non-Replay** | Task in `review` with ambiguous failure diagnostics in `task.result` | `POST /tasks/{id}/resolve` with verified result output | Status `completed`, `completed_at` set, `next_runnable()` returns `None`, verified output persisted |
| **5** | **Forensic Evidence Preservation** | Task in `review` with `{ambiguous: true, session_id: 's1'}` in `result` | `POST /tasks/{id}/resolve` | Output stored under `result.verified_output`; prior diagnostics under `result.prior_execution` |
| **6** | **Concurrent Race Conflict** | Task in `review` at `generation=2` | Two concurrent requests: `POST /approve` vs `POST /resolve` | Exactly one returns `200`, second returns `409 Conflict`. Winning row has `generation=3` |
| **7** | **Generation Mismatch** | Task in `review` at `generation=2` | `POST /tasks/{id}/approve` with `expected_generation=1` | Returns `HTTP 409 Conflict`, no state change |
| **8** | **Retry Budget Reset** | Task in `review` with `retry_count=3` | `POST /tasks/{id}/approve` with `reset_retries=True` | `retry_count` is reset to `0` |
| **9** | **Exhausted Retry Rejection** | Task in `review` with `retry_count=3`, `max_retries=3` | `POST /tasks/{id}/approve` with `reset_retries=False` | Returns `HTTP 422 Unprocessable Entity` |
| **10** | **Guarded Classification Update** | Task in `review` with `side_effect_classification='UNKNOWN'` | `POST /tasks/{id}/approve` with valid `attestation` for `IDEMPOTENT` | Classification updated to `IDEMPOTENT`, attestation recorded in audit event |
| **11** | **Missing Attestation Rejection** | Task in `review` with `side_effect_classification='UNKNOWN'` | `POST /tasks/{id}/approve` with attestation justification < 15 chars | Returns `HTTP 422 Unprocessable Entity` |
| **12** | **Status 409 Conflict** | Task in `queued` or `in_progress` | `POST /tasks/{id}/approve`, `/resolve`, `/reject` | Returns `HTTP 409 Conflict` |
| **13** | **Authentication 401** | `operator_token` configured; request without Bearer token | `POST /tasks/{id}/approve` | Returns `HTTP 401 Unauthorized` |
| **14** | **Review Context Inspection** | Task in `review` with prior error, diagnostics, and checkpoints | `GET /tasks/{id}/review-context` | Returns full aggregated package: task, worker profile, prior diagnostics, event history |
