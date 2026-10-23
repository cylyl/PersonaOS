# Specification: Security Policy Enforcement & Evaluation Engine

**Module:** `personaos.security`
**Related ADR:** ADR 0020, ADR 0004, ADR 0018, ADR 0019
**Source of Truth:** `schemas/policy.yaml`, `schemas/event.yaml`

---

## 1. Architecture & Component Interaction

```
                        ┌────────────────────────┐
                        │ WorkloadScheduler      │
                        │ (Skill Matching)       │
                        └──────────┬─────────────┘
                                   │ find_candidate()
                                   ▼
                        ┌────────────────────────┐
                        │ WorkerExecutionKernel  │
                        └──────────┬─────────────┘
                                   │
              ┌────────────────────┴────────────────────┐
              ▼                                         ▼
   1. Quarantine Check                       2. Pre-Execution Policy
      (Worker.status != 'quarantined')          (policy.evaluate())
              │                                         │
              ├───────────► ALLOW ──────────────────────┤
              ├───────────► DENY ──► Fail Task          │
              └───────────► REQUIRE_APPROVAL            │
                                  │                     │
                                  ▼                     │
                        approval.request()              │
                        (task.status = 'review')        │
                                                        ▼
                                                3. Adapter Execution
                                                   (adapter.execute())
                                                        │
                                                        ▼
                                                4. Tool Policy Check
                                                   (tool_policy.check())
                                                        │
                                                        ├─► ALLOW
                                                        ├─► DENY ──► Raise Error
                                                        └─► REQUIRE_APPROVAL
```

---

## 2. Policy Engine Specification (`src/personaos/security/policy.py`)

### 2.1 Data Models
- **`Effect`**: Enum `[ALLOW, DENY, REQUIRE_APPROVAL]`
- **`Rule`**:
  - `id: str`
  - `description: str | None`
  - `action: str` (glob pattern, e.g. `tool:*`, `task:execute`, `db:read:*`)
  - `resource: str` (glob pattern, e.g. `prod-*`, `*`)
  - `worker_id: str | None` (optional glob pattern)
  - `effect: Effect`
  - `conditions: dict[str, Any] | None` (supports `time_window`, `source_ip`)
- **`Policy`**:
  - `id: str`
  - `name: str`
  - `description: str | None`
  - `rules: list[Rule]`
  - `default_effect: Effect` (default: `DENY` — fail-closed)
  - `priority: int` (higher priority evaluated first)
  - `applies_to: dict[str, Any] | None` (`worker_roles`, `worker_ids`, `task_types`)
- **`Decision`**:
  - `effect: Effect`
  - `policy_id: str | None`
  - `rule_id: str | None`
  - `reason: str`
  - `matched: bool`

### 2.2 Evaluation Algorithm
1. If no policies are provided or applicable, evaluate against fallback default effect (`DENY`).
2. Sort applicable policies by `priority DESC, id ASC`.
3. For each policy:
   - For each rule in `policy.rules`:
     - Match `action` using `fnmatchcase(action, rule.action)`.
     - Match `resource` using `fnmatchcase(resource, rule.resource)`.
     - If `rule.worker_id` is set, match using `fnmatchcase(actor.id, rule.worker_id)`.
     - If conditions are specified (e.g., `source_ip`), verify condition satisfaction.
     - On first full match: return `Decision(effect=rule.effect, policy_id=policy.id, rule_id=rule.id, reason=f"Matched rule {rule.id}")`.
4. If no rules match in an applicable policy, apply `policy.default_effect`.

---

## 3. Tool Policy Enforcement (`src/personaos/security/tool_policy.py`)

- **Signature:**
  `ToolPolicy.check(worker: Worker, tool_name: str, tool_args: dict[str, Any], context: dict[str, Any] | None = None) -> Decision`
- **Enforcement Rules:**
  1. Action mapped to `tool:<tool_name>`.
  2. If `worker.capabilities` is non-empty and does NOT contain `tool_name` or `tool:<tool_name>` or `*`, reject immediately with `DENY`.
  3. Evaluate through `PolicyEngine.evaluate(actor=worker, action=f"tool:{tool_name}", resource=resource, context=context)`.
  4. Returns `Decision`. If `DENY`, caller raises `PermissionDeniedError` and emits `policy.denied`. If `ALLOW`, emits `policy.allowed`.

---

## 4. Human Approval Gate (`src/personaos/security/approval.py`)

- **`request(session, task, reason, approvers, policy_id, rule_id)`**:
  - Transitions `task.status = 'review'` and `exhaustion_reason = 'approval_required'`.
  - Increments `task.generation = task.generation + 1`.
  - Clears `lock_expires_at = None`.
  - Emits `approval.requested` event to `task_events` with `approvers`, `policy_id`, `rule_id`.
- **`approve(session, task_id, operator_id, expected_generation, reason)`**:
  - Delegates to `TaskReviewService.approve()`, transitioning task back to `queued`.
- **`reject(session, task_id, operator_id, expected_generation, reason)`**:
  - Delegates to `TaskReviewService.reject()`, transitioning task to `failed`.

---

## 5. Worker Quarantine (`src/personaos/security/quarantine.py`)

- **`quarantine(session, worker_id, reason, quarantined_by)`**:
  - Updates `Worker.status = 'quarantined'`.
  - Drains active bindings in `worker_nodes`.
- **`unquarantine(session, worker_id, reason)`**:
  - Updates `Worker.status = 'idle'`.
- **Scheduler & Kernel Checks:**
  - `WorkloadScheduler` excludes workers with `status = 'quarantined'`.
  - `WorkerExecutionKernel` checks worker status before claim; raises `WorkerNotEligibleError` if worker is quarantined.

---

## 6. Secrets Isolation (`src/personaos/security/secrets.py`)

- Resolves `secret:<NAME>` from environment variables or custom secret providers.
- Scans and redacts secret values from outgoing event payloads, logs, and diagnostic dictionaries.

---

## 7. STRIDE Threat Model

| Threat | Component | Vulnerability | Mitigation |
|---|---|---|---|
| **Spoofing** | Kernel / Runner | Rogue worker attempts to claim another worker's task. | Atomic SQL affinity fencing in claim query (`assigned_worker IS NULL OR assigned_worker = :worker_id`). |
| **Tampering** | Tool Execution | Task manipulates tool arguments to access unapproved paths. | Tool policy engine evaluates resource globs and argument schemas; rejects unauthorized scopes. |
| **Repudiation** | Audit Log | Security decisions executed without audit trail. | Transactional emission of `policy.allowed`, `policy.denied`, and `approval.requested` in `task_events`. |
| **Information Disclosure** | Secrets / Events | Plaintext tokens stored in task result or event payloads. | Runtime resolution via `security/secrets.py` + recursive payload sanitization/redaction. |
| **Denial of Service** | Approval Queue | Infinite blocked tasks without operator intervention. | `APPROVAL_TIMEOUT_SECONDS` triggers timeout failure on expired pending approvals. |
| **Elevation of Privilege** | Permissions | Worker requests unauthorized tool invocation. | Fail-closed policy evaluation; worker capabilities allowlist checked before execution. |

---

## 8. Test Acceptance Matrix

1. **Unit Tests (`tests/unit/security/`):**
   - Policy evaluation: exact matches, globs (`*`, `?`), priority overrides, fail-closed defaults.
   - Rule condition matching (time window, source IP).
   - Tool policy allow/deny/require-approval decisions.
   - Secrets resolution and string/dict redaction.
2. **Integration Tests (`tests/integration/test_security_policy.py`):**
   - Quarantined worker blocked from candidate selection and kernel claim.
   - Pre-execution policy deny fails task immediately with `policy.denied` event.
   - Pre-execution policy `require_approval` pauses task in `review` with `approval.requested` event.
   - Operator resolution (`approve`/`reject`) resumes or fails approval-gated tasks.
   - Tool execution enforcement with audited events.
   - Migration 0006 upgrade/downgrade validation.
