# ADR 0019: Worker Skill, Capability, and Affinity Scheduling

**Status:** Proposed (Step 16 Design Phase)
**Date:** 2026-10-23
**Deciders:** PersonaOS Architecture Team
**Consulted:** Control Plane, Workload Execution, Reliability, Security
**Informed:** Operators, Runtime Adapters

---

## Context & Problem Statement

PersonaOS models workers as persistent identities with versioned profiles, specific skills, and defined capabilities. In Steps 5 through 15, the core execution pipeline was established:
- **ADR 0008 / ADR 0009:** Worker identity is separated from execution nodes; worker profiles are immutable YAML snapshots (`tasks.profile_version`).
- **ADR 0010:** `WorkerExecutionKernel` owns the atomic claim (`queued → in_progress`), lease heartbeats, and terminal transitions.
- **ADR 0012 / ADR 0013:** `WorkerOrchestrator` polls `WorkloadService.next_runnable()` for queued tasks with satisfied dependencies.
- **ADR 0016 / ADR 0017 / ADR 0018:** Leases, ambiguous failure isolation, and operator review resolution safeguard tasks from silent loss and duplicate side effects.

### The Problem

While `WorkloadService.next_runnable()` correctly enforces dependency prerequisites (ADR 0013), it currently operates as a global, worker-agnostic FIFO query:
```sql
SELECT ... FROM tasks
WHERE status = 'queued'
  AND (jsonb_array_length(dependencies) = 0 OR NOT EXISTS (...))
ORDER BY created_at ASC, id ASC
LIMIT 1
```

This presents two critical operational failures in multi-worker environments:
1. **Worker Affinity Violation & Execution Crashes:**
   If Task A is explicitly assigned to Worker 1 (`tasks.assigned_worker = 'worker-1'`), but Worker 2 polls `next_runnable()`, Worker 2 receives Task A. When Worker 2 passes Task A to `WorkerExecutionKernel.execute()`, `ProfileRegistry.resolve_execution_context` attempts to load Worker 1's profile using Worker 2's context or encounters worker mismatches. Furthermore, the kernel's atomic claim SQL currently lacks worker fencing, allowing Worker 2 to overwrite `tasks.assigned_worker`.
2. **Head-of-Line (HoL) Blocking & Skill Incompatibility:**
   If a task at the head of the FIFO queue requires skills (e.g. `["kubernetes", "helm"]`) that the polling worker lacks, a worker-agnostic query repeatedly returns that task to incompatible workers. The workers either repeatedly reject the claim or fail during execution, starving compatible tasks queued behind it.
3. **Unimplemented Scheduler Stub:**
   `src/personaos/workload/scheduler.py` is currently an empty stub with a TODO comment dating back to project bootstrap.

The database schema (Migration 0001) and schemas (`schemas/worker.yaml`, `schemas/task.yaml`) already define `tasks.required_skills`, `tasks.required_capabilities`, `tasks.assigned_worker`, `tasks.assigned_node`, `tasks.priority`, `workers.capabilities`, `workers.permissions`, `worker_skills`, and `worker_nodes`. However, no component currently evaluates these requirements during task discovery.

---

## Decision

We will implement `WorkloadScheduler` in `src/personaos/workload/scheduler.py` to perform SQL-based, worker-aware candidate selection, while strictly maintaining the architectural invariants of PersonaOS.

### 1. Architectural Boundary: Read-Only Selection vs. Atomic Claim

- **Candidate Selection is Strictly Read-Only:**
  `WorkloadScheduler.find_candidate(worker_id: str, node_id: str | None = None)` selects eligible tasks matching the requesting worker. It executes a read-only query in PostgreSQL. It does **not** mutate task status, does **not** set worker assignments, and does **not** acquire row locks (`FOR UPDATE`).
- **Kernel Retains Exclusive Claim Authority:**
  `WorkerExecutionKernel` remains the sole owner of atomic task claims.
  To prevent race conditions where an ineligible worker attempts to claim a task, the kernel's atomic claim SQL predicate is updated to enforce worker affinity:
  ```sql
  UPDATE tasks
  SET    status = 'in_progress',
         started_at = NOW(),
         generation = generation + 1,
         lock_expires_at = NOW() + INTERVAL ':lease_ttl seconds',
         assigned_worker = :worker_id,
         assigned_node = :node_id
  WHERE  id = :task_id
    AND  status = 'queued'
    AND  (assigned_worker IS NULL OR assigned_worker = :worker_id)
  RETURNING generation, assigned_worker
  ```
  If another runner claims the task between selection and claim, `rowcount == 0` and the kernel raises `TaskStatusError`. The calling orchestrator handles this as a standard claim conflict (`claim_conflicts += 1`) and continues polling.

### 2. Worker Affinity Rules

Candidate selection partitions tasks into three affinity categories:
1. **Explicitly Assigned Tasks (`tasks.assigned_worker = :worker_id`):**
   Eligible *only* for the assigned worker. Excluded from all other workers.
2. **Unassigned Tasks (`tasks.assigned_worker IS NULL`):**
   Eligible for any active worker that possesses all required skills and capabilities. Upon claim, the claiming worker is recorded as `assigned_worker`, and `tasks.profile_version` is snapshotted to `claiming_worker.current_profile_version`.
3. **Tasks Assigned to Another Worker (`tasks.assigned_worker IS NOT NULL AND tasks.assigned_worker != :worker_id`):**
   Ineligible and filtered out by SQL.

### 3. Skill Matching Semantics

- **Task Requirements:** `tasks.required_skills` is a JSONB array of lowercase strings (e.g. `'["kubernetes", "terraform"]'::jsonb`).
- **Worker Skills Source of Truth:**
  Worker skills are stored in the relational join table `worker_skills(worker_id, skill)`.
  Additionally, for resilience and profile consistency, skills declared in `WorkerProfile.skills` (synced to `workers.capabilities` or `worker_skills`) are normalized to lowercase.
- **Matching Rule:**
  A worker is eligible for a task if the task's required skills are a subset of the worker's skills:
  - If `tasks.required_skills` is empty (`'[]'::jsonb` or length 0): **Always matches** (no skill requirement).
  - If `tasks.required_skills` is non-empty: **All** required skills must be possessed by the worker (set containment).
  - In SQL:
    ```sql
    (
      jsonb_array_length(tasks.required_skills) = 0
      OR NOT EXISTS (
        SELECT 1
        FROM jsonb_array_elements_text(tasks.required_skills) AS req_skill
        WHERE req_skill NOT IN (
          SELECT skill FROM worker_skills WHERE worker_id = :worker_id
          UNION
          SELECT jsonb_array_elements_text(capabilities) FROM workers WHERE id = :worker_id
        )
      )
    )
    ```

### 4. Capability and Node Matching Semantics

- **Capability Requirements:** `tasks.required_capabilities` is a JSONB array of tool identifiers (e.g. `'["kubectl", "aws"]'::jsonb`).
- **Worker Capabilities Source of Truth:** `workers.capabilities` is a JSONB array of allowed tools on the `workers` table.
- **Matching Rule:**
  `tasks.required_capabilities <@ workers.capabilities`
  If `tasks.required_capabilities` is empty (`'[]'::jsonb`), it matches any worker.
- **Node Matching:**
  There is no independent `nodes` table in v0.1; nodes are represented as execution runners bound via `worker_nodes`.
  If `tasks.assigned_node` is set, it must match the runner's active `node_id`. If `tasks.assigned_node IS NULL`, any active node is eligible.

### 5. Priority and FIFO Compatibility

`schemas/task.yaml` specifies:
> `priority`: enum `[low, medium, high, critical]`, default `medium`.
> "Within priority, FIFO (oldest first)."

To align the roadmap specification with existing FIFO queue guarantees:
1. `WorkloadScheduler` orders candidates by priority rank first, then FIFO:
   ```sql
   ORDER BY
     CASE tasks.priority
       WHEN 'critical' THEN 4
       WHEN 'high'     THEN 3
       WHEN 'medium'   THEN 2
       WHEN 'low'      THEN 1
       ELSE 2
     END DESC,
     tasks.created_at ASC,
     tasks.id ASC
   ```
2. **Preservation of Existing Tests:**
   For all tasks sharing the default priority (`'medium'`), the ordering is strictly `created_at ASC, id ASC` (identical to ADR 0013).
   `WorkloadService.next_runnable()` remains untouched as the baseline un-filtered dependency inspection query, preventing any regressions in existing test suites.

### 6. Dependency Safety & Starvation Prevention

- The scheduler incorporates the proven dependency satisfaction logic from ADR 0013:
  `jsonb_array_length(dependencies) = 0` OR every dependency ID resolves to a task with `status = 'completed'`. Missing or failed dependencies render the task ineligible.
- Filtering by worker affinity, skills, and capabilities in SQL directly prevents Head-of-Line blocking: incompatible tasks are not returned to the requesting worker, allowing the worker to claim the oldest compatible task without starvation.
- The scheduler supports `find_candidates(worker_id, node_id, limit=1)` returning a bounded list of candidates (default 1) to support efficient polling without cursor state.

### 7. Explicit Security Separation

Skill matching matches *functional competence* (can the worker perform the work?). It is explicitly **not** security policy authorization or RBAC. Final execution policy checks remain governed by the security layer surrounding runner actions (ADR 0004), leaving existing permissive stubs unaffected.

---

## Consequences

### Positive
- **Multi-Worker Safety:** Different worker roles (e.g. DevOps vs QA) can poll concurrently without cross-claiming or corrupting worker affinity.
- **Zero Head-of-Line Blocking:** Workers skip tasks they lack skills for; tasks are matched to qualified workers.
- **Strict Invariant Preservation:** `WorkerExecutionKernel` remains the sole atomic claim owner. Candidate selection is purely read-only.
- **Deterministic Priority & FIFO:** Higher priority tasks run first; equal-priority tasks preserve strict FIFO.
- **Zero Schema Migrations:** All required tables (`tasks`, `workers`, `worker_skills`, `worker_nodes`) and columns already exist in Migration 0001.

### Negative / Trade-offs
- The candidate selection query performs JSONB array containment and subqueries; for high-throughput queues, functional or GIN indexing on `tasks.required_skills` and `tasks.required_capabilities` may be beneficial in future scale milestones.
