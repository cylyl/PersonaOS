# ADR 0020: Security Policy Enforcement, Tool Policies, Approval Gates, and Worker Quarantine

**Status:** Accepted (Step 17 Implementation Phase)
**Date:** 2026-10-23
**Deciders:** PersonaOS Architecture Team
**Consulted:** Control Plane, Workload Execution, Reliability, Security
**Informed:** Operators, Runtime Adapters

---

## Context & Problem Statement

Per ADR 0004 ("Security is a primitive, not a feature"), PersonaOS requires that security surrounds every action in the worker execution loop. In previous milestones (Steps 1 through 16):
- Worker identities, versioned YAML profiles, and execution nodes were established (ADRs 0003, 0008, 0009).
- Atomic claims, lease heartbeats, and generation-fenced terminal transitions were locked into `WorkerExecutionKernel` (ADRs 0010, 0016).
- Worker skill and capability matching was introduced into candidate scheduling (ADR 0019).
- Ambiguous execution isolation and operator re-entry lifecycle were formalized (ADR 0018).

However, `src/personaos/security/` remained populated with TODO stubs (`policy.py`, `tool_policy.py`, `approval.py`, `permissions.py`, `quarantine.py`, `secrets.py`). As highlighted in `VIKUNJA.md`, tasks and tools currently execute without fail-closed policy evaluation, human approval gates, or tool authorization checks. Furthermore:
1. **Permissive Defaults & Lack of Fail-Closed Evaluation**:
   Without an active policy engine, tasks and tool calls run unconstrained or bypass checks. If a policy is missing, malformed, or undefined, access must fail-closed (default: DENY).
2. **Tool Authorization vs. Functional Skills**:
   ADR 0019 explicitly separated functional skills (can the worker perform the work?) from authorization (is the worker permitted to invoke tool X on resource Y?). Tool invocation requires fine-grained policy checks close to execution.
3. **Approval Lifecycle Disconnection**:
   When policy evaluation yields `REQUIRE_APPROVAL`, the task must be safely paused and enqueued for operator sign-off using the generation-fenced review/approval lifecycle rather than running unverified.
4. **Worker Quarantine Enforcement**:
   While `WorkerModel.status` includes `'quarantined'`, there was no domain service to transition a worker into/out of quarantine, and execution components did not strictly block quarantined workers from claiming work.
5. **Secrets Leakage Risks**:
   Secret references (e.g. `secret:KEY_NAME`) must resolve at runtime without persisting or leaking plaintext values in prompts, checkpoints, or `task_events`.

---

## Decision

Implement complete, fail-closed **Security Policy Enforcement** adhering to `schemas/policy.yaml`, integrating with `WorkerExecutionKernel`, `WorkerOrchestrator`, and `TaskReviewService`.

### 1. Source of Truth & Policy Schema (`schemas/policy.yaml`)

Policies are modeled as first-class domain entities loaded from YAML files or dictionaries conforming strictly to `schemas/policy.yaml`:
- **Policy Entity**: `id`, `name`, `priority` (integer >= 0), `default_effect` (`allow`, `deny`, `require_approval`), `rules`, `applies_to` (worker_roles, worker_ids, task_types).
- **Rule Entity**: `id`, `action` (glob pattern, e.g. `tool:*`, `db:read:*`), `resource` (glob pattern), `worker_id` (glob pattern or None), `effect` (`allow`, `deny`, `require_approval`), and optional `conditions` (time_window, source_ip).
- **Evaluation Order**:
  1. Filter applicable policies by matching `applies_to` against the requesting worker and task.
  2. Order applicable policies by `priority DESC, id ASC`.
  3. Within each policy, evaluate rules in top-to-bottom order (`first match wins`).
  4. If no rule matches across applicable policies, apply `default_effect` of the highest-priority policy (or system default: `DENY` — fail-closed).

### 2. Policy Evaluation Engine (`src/personaos/security/policy.py`)

- Exposes `evaluate(actor, action, resource, context) -> Decision`.
- `Decision` dataclass carries `effect: Effect`, `policy_id: str | None`, `rule_id: str | None`, and `reason: str`.
- Matching uses `fnmatchcase` for strict, case-sensitive glob comparison.
- Evaluator is thread-safe and stateless, capable of loading policies from disk (`policies/`) or memory.

### 3. Tool Policy Enforcement (`src/personaos/security/tool_policy.py`)

- Exposes `ToolPolicy.check(worker, tool_name, tool_args, context) -> Decision`.
- Translates tool invocation to action `tool:<tool_name>`.
- Evaluates against worker capabilities allowlist and active policies.
- Audit event emission:
  - Allowed: emits `policy.allowed` with `task_id`, `worker_id`, `policy_id`, `rule_id`.
  - Denied: raises `PermissionDeniedError` and emits `policy.denied` with diagnostic reason.
  - Require approval: returns `REQUIRE_APPROVAL` decision to pause execution.

### 4. Human Approval Gates (`src/personaos/security/approval.py`)

- When pre-execution policy or tool check returns `REQUIRE_APPROVAL`:
  - `approval.request(session, task, reason, approvers)`:
    - Atomically transitions task status to `'review'` with `exhaustion_reason = 'approval_required'`.
    - Emits `approval.requested` event with `task_id`, `policy_id`, and `approvers`.
    - Increments generation to invalidate in-flight leases.
- Approvals leverage `TaskReviewService` (ADR 0018):
  - Operator calls `/tasks/{id}/approve` to re-queue the task for execution with generation fencing.
  - Operator calls `/tasks/{id}/reject` to terminally fail the task.

### 5. Worker Quarantine Management (`src/personaos/security/quarantine.py`)

- `quarantine(session, worker_id, reason, quarantined_by)`:
  - Atomically updates `WorkerModel.status = 'quarantined'`.
  - Clears active bindings in `worker_nodes`.
- `unquarantine(session, worker_id, reason)`:
  - Transitions `WorkerModel.status = 'idle'`.
- Enforcement:
  - `WorkloadScheduler` filters `Worker.status != 'quarantined'`.
  - `WorkerExecutionKernel` validates worker eligibility before claim; raises `WorkerNotEligibleError` if quarantined.

### 6. Secrets Isolation & Redaction (`src/personaos/security/secrets.py`)

- `SecretsManager.resolve(secret_ref)`:
  - Matches `secret:<NAME>` references and resolves from environment variables or vault mappings.
- `SecretsManager.redact(text_or_dict)`:
  - Recursively scans outputs, error messages, and event payloads to scrub sensitive patterns and resolved secret values.
  - Ensures secret values are never logged or stored in `task_events`.

### 7. Database Migration 0006 (`0006_security_events.py`)

Updates `task_events.type` check constraint to include security lifecycle events:
```sql
ALTER TABLE task_events DROP CONSTRAINT task_events_type_check;
ALTER TABLE task_events ADD CONSTRAINT task_events_type_check
  CHECK (type IN (
    'task.created', 'task.claimed', 'task.checkpointed',
    'task.completed', 'task.failed', 'task.requeued',
    'policy.allowed', 'policy.denied', 'approval.requested'
  ));
```
Downgrade safely remaps security events to `task.failed` with payload preserving original event details before restoring the 6-element constraint.

---

## Architectural Invariants Preserved

1. **Kernel Supremacy**: `WorkerExecutionKernel` remains the sole authority for atomic claims (`queued → in_progress`) and lifecycle transitions.
2. **Generation Fencing**: Leases and review re-entry preserve strict generation guards (`generation = generation + 1`).
3. **Separation of Skills and Authorization**: Functional skill matching (ADR 0019) guides candidate discovery; security policy evaluation strictly governs execution and tool invocation.
4. **No External Infrastructure**: PostgreSQL remains the sole durable store.

---

## Consequences

### Positive
- Closes technical debt in `src/personaos/security/` stubs.
- Provides strict fail-closed authorization across task execution and tool usage.
- Prevents secret exfiltration via runtime reference resolution and payload redaction.
- Integrates seamlessly with ADR 0018 review lifecycle.

### Negative / Trade-offs
- Policy evaluation adds microseconds per execution loop iteration.
- Requires migration 0006 to expand `task_events.type` constraint.
