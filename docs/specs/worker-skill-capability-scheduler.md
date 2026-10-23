# Worker Skill, Capability, and Affinity Scheduler — Specification

**Status:** Proposed (Step 16 Design Phase)
**Parent ADR:** [ADR 0019 — Worker Skill, Capability, and Affinity Scheduling](../adr/0019-worker-skill-capability-scheduler.md)
**Companion Documents:** [ADR 0008](../adr/0008-thin-worker-six-tables.md), [ADR 0010](../adr/0010-worker-execution-kernel.md), [ADR 0012](../adr/0012-worker-orchestrator.md), [ADR 0013](../adr/0013-workload-runnable-semantics.md)
**Scope:** SQL candidate selection, worker affinity matching, skill and capability verification, priority ordering, and atomic claim fencing.

---

## 1. Architectural Concept

Step 16 provides the missing scheduler primitive specified in `VIKUNJA.md`:
> *Scheduler: FIFO + priority + skill-match in SQL. No AI scheduler.*
> *Scheduler matches Worker to Task (skill + permission + availability + node capability + workload).*

The scheduler acts strictly as a **read-only candidate selector**. It finds queued tasks that a specific worker is qualified and authorized to execute, ordered by priority and FIFO age. The `WorkerExecutionKernel` retains exclusive ownership of the atomic state transition (`queued → in_progress`).

```text
WorkerRunner (worker_id, node_id)
      ↓
WorkerOrchestrator.process_one()
      ↓
WorkloadScheduler.find_candidate(worker_id, node_id) [READ-ONLY SQL]
      ↓  (Candidate Task | None)
WorkerExecutionKernel.execute(task)
      ↓  (Atomic UPDATE WHERE status='queued' AND (assigned_worker IS NULL OR assigned_worker=:worker_id))
Runtime Adapter (OpenClaw / Simulation)
```

---

## 2. Component Responsibilities

| Component | Responsibility | Allowed State Changes |
|---|---|---|
| **`WorkloadScheduler`** | Queries PostgreSQL for runnable tasks matching worker affinity, skills, capabilities, node, and deadline. | **None (strictly read-only)** |
| **`WorkerOrchestrator`** | Polling loop. Requests candidate from scheduler for its assigned `worker_id` and invokes kernel. | Metrics tracking (`claim_conflicts`, `tasks_processed`) |
| **`WorkerExecutionKernel`** | Owns the atomic claim, lease extension, generation increment, and terminal status transitions. | `queued → in_progress`, `in_progress → completed/failed/review` |
| **`WorkloadService`** | Task ingestion (`enqueue`), dependency resolution, review queue routing, and general task inspection. | Inserts new tasks, records recovery routing |

---

## 3. SQL Query Specification

### 3.1 Candidate Selection Query

The canonical candidate selection query executed by `WorkloadScheduler.find_candidate`:

```sql
SELECT t.id, t.title, t.objective, t.status, t.priority, t.type,
       t.required_skills, t.required_capabilities,
       t.assigned_worker, t.assigned_node,
       t.dependencies, t.input, t.checkpoint_ref, t.result,
       t.retry_count, t.max_retries, t.last_error,
       t.deadline, t.created_at, t.started_at, t.completed_at,
       t.profile_version, t.lock_expires_at, t.generation,
       t.side_effect_classification, t.exhaustion_reason
FROM tasks t
WHERE t.status = 'queued'
  -- 1. Worker Affinity: unassigned OR pre-assigned to this worker
  AND (t.assigned_worker IS NULL OR t.assigned_worker = :worker_id)
  -- 2. Node Affinity: unassigned OR pre-assigned to this node
  AND (:node_id IS NULL OR t.assigned_node IS NULL OR t.assigned_node = :node_id)
  -- 3. Deadline Gate: unexpired tasks only
  AND (t.deadline IS NULL OR t.deadline > NOW())
  -- 4. Dependency Gate: all declared dependencies must be completed
  AND (
      jsonb_array_length(t.dependencies) = 0
      OR NOT EXISTS (
          SELECT 1
          FROM jsonb_array_elements_text(t.dependencies) AS dep_id
          LEFT JOIN tasks dep ON dep.id = dep_id
          WHERE dep.id IS NULL              -- missing dependency
             OR dep.status <> 'completed'   -- dependency incomplete
      )
  )
  -- 5. Capability Match: all required capabilities must be in worker capabilities
  AND (
      jsonb_array_length(t.required_capabilities) = 0
      OR t.required_capabilities <@ (
          SELECT COALESCE(capabilities, '[]'::jsonb)
          FROM workers
          WHERE id = :worker_id
      )
  )
  -- 6. Skill Match: all required skills must be possessed by worker
  AND (
      jsonb_array_length(t.required_skills) = 0
      OR NOT EXISTS (
          SELECT 1
          FROM jsonb_array_elements_text(t.required_skills) AS req_skill
          WHERE req_skill NOT IN (
              SELECT skill FROM worker_skills WHERE worker_id = :worker_id
              UNION
              SELECT jsonb_array_elements_text(capabilities) FROM workers WHERE id = :worker_id
          )
      )
  )
ORDER BY
  CASE t.priority
    WHEN 'critical' THEN 4
    WHEN 'high'     THEN 3
    WHEN 'medium'   THEN 2
    WHEN 'low'      THEN 1
    ELSE 2
  END DESC,
  t.created_at ASC,
  t.id ASC
LIMIT :limit;
```

### 3.2 Fenced Atomic Claim Query

The atomic claim executed exclusively by `WorkerExecutionKernel`:

```sql
UPDATE tasks
SET    status = 'in_progress',
       started_at = NOW(),
       generation = generation + 1,
       lock_expires_at = NOW() + INTERVAL ':ttl_seconds seconds',
       assigned_worker = :worker_id,
       assigned_node = :node_id
WHERE  id = :task_id
  AND  status = 'queued'
  AND  (assigned_worker IS NULL OR assigned_worker = :worker_id)
RETURNING generation, assigned_worker;
```

---

## 4. Matching Rules & Domain Semantics

### 4.1 Worker Affinity
1. `assigned_worker = NULL`: Candidate is open to any active worker satisfying skills and capabilities.
2. `assigned_worker = 'worker-devops-01'`: Candidate is strictly restricted to `'worker-devops-01'`.
3. `assigned_worker = 'worker-qa-01'`: Candidate is completely excluded from `'worker-devops-01'`.

### 4.2 Skill Matching
- **Format:** `required_skills` is a JSONB array of strings.
- **Normalization:** Strings are trimmed and lowercased.
- **Evaluation:** Strict subset containment. Every skill in `required_skills` must be present in the worker's skills.
- **Empty / Null:** An empty array `[]` matches all workers.
- **Missing Skills:** If any required skill is missing, the task is skipped without failing or locking.

### 4.3 Capability Matching
- **Format:** `required_capabilities` is a JSONB array of strings.
- **Evaluation:** Evaluated against `workers.capabilities` using PostgreSQL JSONB containment `<@`.
- **Empty / Null:** An empty array `[]` matches all workers.

### 4.4 Priority Ordering
- Priority ranks: `critical` (4) > `high` (3) > `medium` (2) > `low` (1).
- Unrecognized or null priority defaults to `medium` (2).
- Tie-breaking: For tasks of equal priority, ordering is strictly deterministic FIFO: `created_at ASC, id ASC`.

---

## 5. Concurrency and Failure Handling

1. **Claim Race Between Runners:**
   If Runner A and Runner B discover the same candidate task concurrently, both invoke `kernel.execute()`.
   - The first runner's `UPDATE` succeeds (`rowcount == 1`), setting `status = 'in_progress'`.
   - The second runner's `UPDATE` matches 0 rows (`rowcount == 0`), raising `TaskStatusError`.
   - Runner B catches `TaskStatusError`, logs a claim conflict, and polls again.
2. **Head-of-Line Blocking Protection:**
   Because SQL filters candidate tasks by the querying worker's skills, an incompatible task at the head of the queue is invisible to unqualified workers, preventing queue starvation.
3. **Stale Worker Affinity:**
   If a runner attempts to claim a task pre-assigned to another worker, the kernel claim's `AND (assigned_worker IS NULL OR assigned_worker = :worker_id)` condition prevents any mutation and raises `TaskStatusError`.

---

## 6. Explicit Non-Goals

- **No AI / Heuristic Scheduling:** No machine learning or heuristic scheduling algorithms. All rules are deterministic SQL.
- **No Distributed Queue / Redis:** PostgreSQL remains the sole durable queue.
- **No Background Scheduler Daemon:** Scheduling is on-demand, driven by worker polling.
- **No Dynamic Policy Authorization:** Tool policies, RBAC, and approval gates remain the responsibility of the security layer surrounding runner execution (ADR 0004).

---

## 7. Integration Test Plan & Acceptance Scenarios

The test suite in `tests/integration/test_scheduler.py` validates the following 15 scenarios against PostgreSQL testcontainers:

1. **`test_unassigned_task_matched_to_qualified_worker`**: Unassigned task matching skills is selected.
2. **`test_explicit_affinity_matched_to_assigned_worker`**: Pre-assigned task is selected by the assigned worker.
3. **`test_explicit_affinity_blocks_other_workers`**: Pre-assigned task is invisible to different workers.
4. **`test_required_skills_subset_matching`**: Worker possessing required skills matches; missing skill skips.
5. **`test_empty_required_skills_matches_any_worker`**: Task with `required_skills = []` matches any worker.
6. **`test_required_capabilities_subset_matching`**: Worker possessing tools matches; missing tool skips.
7. **`test_empty_required_capabilities_matches_any_worker`**: Task with `required_capabilities = []` matches.
8. **`test_node_affinity_filtering`**: Task pre-assigned to `node-a` matches `node-a` and skips `node-b`.
9. **`test_priority_overrides_fifo_creation_order`**: `critical` task created later is selected before older `medium` task.
10. **`test_equal_priority_preserves_strict_fifo`**: Two `medium` tasks are returned strictly in `created_at ASC` order.
11. **`test_incomplete_dependency_blocks_selection`**: Task with pending dependency is not selected.
12. **`test_completed_dependency_unblocks_selection`**: Task becomes eligible once all dependencies reach `completed`.
13. **`test_head_of_line_blocking_prevented`**: Unqualified worker skips head task to select matching queued task.
14. **`test_concurrent_runners_serialized_by_kernel_claim`**: Two runners selecting same task produce 1 winner and 1 clean conflict.
15. **`test_candidate_selection_is_read_only`**: Selection query produces zero mutations, locks, or audit events.
