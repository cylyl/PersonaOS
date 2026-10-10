# In-Progress Task Recovery — Specification

**Spec status:** Draft
**Parent ADR:** ADR 0016
**Scope:** v0.2 minimum viable recovery mechanism

## Architecture

### Schema Additions (v0.2)

```sql
-- 4 new columns. All additive. Existing columns untouched.
ALTER TABLE tasks ADD COLUMN lock_expires_at             TIMESTAMPTZ NULL;
ALTER TABLE tasks ADD COLUMN generation                INTEGER      NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN side_effect_classification TEXT        NOT NULL DEFAULT 'UNKNOWN'
  CHECK (side_effect_classification IN ('UNKNOWN', 'IDEMPOTENT', 'HAS_SIDE_EFFECTS'));
ALTER TABLE tasks ADD COLUMN exhaustion_reason          TEXT         NULL;

CREATE INDEX ix_tasks_stale_in_progress
  ON tasks (lock_expires_at)
  WHERE status = 'in_progress' AND lock_expires_at IS NOT NULL;

CREATE INDEX ix_tasks_review_queue
  ON tasks (priority, created_at)
  WHERE status = 'review';
```

The `side_effect_classification` CHECK constraint enforces the canonical three-way vocabulary at the database boundary. Any value outside `UNKNOWN` / `IDEMPOTENT` / `HAS_SIDE_EFFECTS` is rejected by Postgres regardless of caller code.

The `exhaustion_reason` column is distinct from `last_error` (which is set on every failure path). `exhaustion_reason` is set to `'retry_limit_exhausted'` only on the retry-exhaustion path, giving the operator UI an unambiguous signal that this is a crash-recovery retry-exhaustion (not an adapter-reported failure).

| Column | Semantics |
|---|---|
| `lock_expires_at` | Set on claim; refreshed by heartbeat; `NULL` means no active lease |
| `generation` | Monotonically increasing integer; starts at 0, increments on every claim/reclaim |
| `side_effect_classification` | `'UNKNOWN'` = not classified, no auto-reclaim; `'IDEMPOTENT'` = confirmed safe; `'HAS_SIDE_EFFECTS'` = confirmed unsafe |
| `exhaustion_reason` | `NULL` for normal flow; `'retry_limit_exhausted'` on retry-exhaustion path |

### Clock Source

All time comparisons use `NOW()` / `func.now()` (database server time). Worker-local clocks are never used for `lock_expires_at` comparisons.

### TTL and Heartbeat Defaults

| Parameter | Default | Notes |
|---|---|---|
| Lease TTL | 15 minutes | Per-profile override via `WorkerProfile.lease_ttl` |
| Heartbeat interval | TTL / 3 = 5 minutes | Best-effort; missed heartbeat does not trigger orphan detection |
| Orphan detection interval | 30 seconds | Scheduler polls stale tasks periodically |
| Effective worst-case detection | TTL + detection_interval | ~15 min 30 sec with defaults |

## Fencing: Every Worker-Write Must Be Fenced

Fencing applies to every state-changing operation by the worker. Unfenced writes would allow a stale worker to corrupt state after ownership transfer.

### Fencing Invariant

> A worker may only write to a task if `assigned_worker = current_worker_id AND generation = expected_generation` at the time of the write.

### Fenced Operations

| Operation | Fenced? | SQL guard |
|---|---|---|
| `complete()` | **Yes** | `WHERE generation = :gen AND assigned_worker = :wid` |
| `fail()` | **Yes** | `WHERE generation = :gen AND assigned_worker = :wid` |
| `heartbeat` (refresh `lock_expires_at`) | **Yes** | `WHERE generation = :gen AND assigned_worker = :wid` |
| `checkpoint_write()` (update `checkpoint_ref`) | **Yes** | `WHERE generation = :gen AND assigned_worker = :wid` |
| Claim / reclaim | **Yes** | Atomic `UPDATE WHERE lock_expires_at < NOW()` — one winner |
| Operator force-reset | **No** | Intentional bypass; increments `generation` to fence stale writes |

### Fencing Check on Every Write

```python
# Pseudocode for every worker-owned write:
result = await session.execute(
    update(TaskModel)
    .where(
        TaskModel.id == task_id,
        TaskModel.generation == expected_generation,
        TaskModel.assigned_worker == current_worker_id,
    )
    .values(<new_values>)
)
if result.rowcount == 0:
    raise StaleWorkerError(f"task {task_id} was reclaimed; write rejected")
```

## Reclaim Transaction

Reclaim combines orphan detection + ownership transfer in a single atomic SQL statement. Two recovery workers racing on the same task: exactly one wins.

### Reclaim SQL

```sql
UPDATE tasks
SET    status          = 'in_progress',
       assigned_worker = :new_worker_id,
       lock_expires_at = NOW() + :lease_ttl,
       generation      = generation + 1,
       checkpoint_ref  = NULL,   -- clear kernel-level checkpoint
       started_at      = NOW(),
       retry_count     = retry_count + 1
WHERE  id              = :task_id
  AND  status          = 'in_progress'
  AND  generation      = :observed_generation      -- fenced: fails if a recovery-loop
                                                    -- write raced after the read
  AND  lock_expires_at < NOW()
  AND  side_effect_classification = 'IDEMPOTENT'
  AND  retry_count    < max_retries
RETURNING id, generation
```

### Reject Conditions

| Condition | Result |
|---|---|
| `status != 'in_progress'` | 0 rows affected — not reclaimable (already terminal) |
| `generation != :observed_generation` | 0 rows affected — ownership changed since the read; stale recovery decision is a no-op |
| `lock_expires_at >= NOW()` | 0 rows affected — lease still valid |
| `side_effect_classification = 'UNKNOWN'` | 0 rows affected — task not classified; operator must review |
| `side_effect_classification = 'HAS_SIDE_EFFECTS'` | 0 rows affected — confirmed side-effect task; operator required |
| `retry_count >= max_retries` | 0 rows affected — recovery loop falls through to retry-exhaustion path |

### Generation After Reclaim

- Stale worker holds `generation = N`
- Reclaim increments to `generation = N + 1`
- All subsequent writes by stale worker are fenced (expects `N`, finds `N+1`)
- New owner holds `generation = N + 1`
- The recovery worker's reclaim UPDATE includes `WHERE generation = :observed_generation`; if the read observed `N` but the row is already at `N+1` (operator reset or concurrent recovery), the WHERE fails and the UPDATE is a no-op.

## Retry Limits

Each reclaim increments `retry_count`. The retry budget is configured via `max_retries` (default `3`, server-set on the `tasks` table by migration 0001). `max_retries = 0` is permitted and **disables automatic reclamation**; such tasks must be reset by an operator.

The reclaim predicate `retry_count < max_retries` partitions the stale-task set into two non-overlapping sets with the retry-exhaustion predicate `retry_count >= max_retries`. Only one path can affect any given task in a given recovery cycle.

### Retry-Exhaustion Path (Classification-Aware)

When `retry_count >= max_retries` and the reclaim predicate fails, the recovery loop runs the **retry-exhaustion UPDATE**. The destination status depends on `side_effect_classification`:

| Classification | Destination status | `completed_at` | `exhaustion_reason` |
|---|---|---|---|
| `IDEMPOTENT` | `failed` (terminal) | `NOW()` | `'retry_limit_exhausted'` |
| `UNKNOWN` (default) | `review` (operator handoff) | `NULL` | `'retry_limit_exhausted'` |
| `HAS_SIDE_EFFECTS` | `review` (operator handoff) | `NULL` | `'retry_limit_exhausted'` |

The `review` status is **already present in the v0.1 status enum** (`'queued', 'assigned', 'in_progress', 'blocked', 'review', 'completed', 'failed', 'canceled'`, ADR 0008 / migration 0001). No new status value is added; the v0.1 enum is leveraged as the operator-handoff state. This avoids a schema migration for the status type.

**Retry-exhaustion SQL (atomic, classification-aware, fenced):**

```sql
UPDATE tasks
SET    status            = CASE
           WHEN side_effect_classification = 'IDEMPOTENT'      THEN 'failed'
           WHEN side_effect_classification = 'UNKNOWN'         THEN 'review'
           WHEN side_effect_classification = 'HAS_SIDE_EFFECTS' THEN 'review'
         END,
       last_error        = COALESCE(last_error, 'max retries exceeded after crash recovery'),
       exhaustion_reason  = 'retry_limit_exhausted',
       completed_at       = CASE
           WHEN side_effect_classification = 'IDEMPOTENT'      THEN NOW()
           ELSE NULL
         END,
       lock_expires_at    = NULL,
       updated_at         = NOW()
WHERE  id                = :task_id
  AND  status            = 'in_progress'
  AND  generation        = :observed_generation      -- fenced: a stale recovery-loop
                                                    -- decision cannot terminate a task
                                                    -- whose ownership has changed
  AND  lock_expires_at   < NOW()
  AND  retry_count      >= max_retries
  AND  side_effect_classification IN ('IDEMPOTENT', 'UNKNOWN', 'HAS_SIDE_EFFECTS')
RETURNING id, status, generation, side_effect_classification, retry_count, max_retries
```

The recovery loop attempts the reclaim first, and falls back to the retry-exhaustion UPDATE only if the reclaim returned 0 rows. **The two UPDATEs are mutually exclusive on `retry_count` and complementary in scope** — only one of them can succeed for a given stale task in a given recovery cycle.

**Both UPDATEs include `generation = :observed_generation` in the WHERE clause.** The recovery worker reads the task (observing `generation`, `retry_count`, `lock_expires_at`, `side_effect_classification`) and then runs the UPDATE. If any observed value changes between the read and the UPDATE — for example, an operator reset increments `generation` and clears `retry_count`, or a concurrent recovery operation re-claims the task — the WHERE clause fails and the UPDATE affects 0 rows.

**Stale-decision scenario**: recovery worker A reads task at `generation=N`, `retry_count=max_retries`, decides retry-exhaustion is appropriate. Before A's UPDATE lands, operator B resets the task (status→queued, retry_count→0, generation→N+1). Then worker C re-claims (status→in_progress, generation→N+2, retry_count=1). Worker A's retry-exhaustion UPDATE: `generation = N` no longer matches (now N+2), 0 rows. Correctly no-op. Without the generation fence, A's UPDATE could match on `status='in_progress'` and `retry_count>=max_retries` if subsequent crashes had re-elevated `retry_count`; the fence eliminates that ambiguity.

The status and `lock_expires_at` predicates remain in the WHERE clause as additional defenses (a worker that has just reclaimed or an operator who is mid-reset cannot have its row hijacked by an in-flight retry-exhaustion). PostgreSQL's row-level lock + WHERE re-evaluation after lock wait provides the same guarantee as in the reclaim path.

### No Silent Discard

The retry-exhaustion path is **never silent**:

- An `IDEMPOTENT` task that exhausts retries transitions to `failed` with `exhaustion_reason = 'retry_limit_exhausted'`. The caller may treat the failure as final and re-enqueue if needed.
- A `UNKNOWN` task that exhausts retries transitions to `review`. The operator can see the task via `SELECT … WHERE status = 'review'`, classify it explicitly, and reset it.
- A `HAS_SIDE_EFFECTS` task that exhausts retries transitions to `review`. The operator must assess the partial external state and decide whether to retry, compensate, or give up.

The `last_error` and `exhaustion_reason` columns both provide a paper trail.

**Relationship to adapter-level retry**: The existing `retry_count`/`max_retries` fields track total execution attempts. v0.2 reuses them for crash-recovery retry counting. Adapter failures (e.g. network timeout) that occur within an execution are separate from crash recovery and are handled by the adapter's own retry logic (not tracked in `retry_count` for v0.2).

## Checkpoint Recovery

On reclaim:
1. `checkpoint_ref` is set to `NULL`.
2. Recovery execution starts **from scratch** — adapter's `execute()` is called with `checkpoint=None`.
3. The adapter is responsible for its own resume mechanism if needed (e.g. reading its own state from an external store). The kernel does not provide a safe kernel-level checkpoint on reclaim because a stale worker's pre-crash checkpoint may reflect invalid partial side effects.

**Rationale**: Starting fresh is the safest default. The operator must classify the task (`side_effect_classification`) before it is eligible for auto-reclaim. If the task has side effects, the operator sets `side_effect_classification = 'HAS_SIDE_EFFECTS'` and manually assesses the resume point before resetting.

## Heartbeat

The heartbeat is a periodic fenced refresh of `lock_expires_at`:

```python
result = await session.execute(
    update(TaskModel)
    .where(
        TaskModel.id == task_id,
        TaskModel.generation == expected_generation,
        TaskModel.assigned_worker == current_worker_id,
        TaskModel.status == "in_progress",
    )
    .values(lock_expires_at=func.now() + lease_ttl)
)
if result.rowcount == 0:
    raise StaleWorkerError("task was reclaimed; stopping heartbeat")
```

- Best-effort: if the heartbeat fails (network, DB), the next heartbeat retry will detect the `StaleWorkerError` and stop.
- After `StaleWorkerError`: worker must stop the execution loop and discard any pending result.

## Recovery Policy Matrix

The classification values are exactly the three allowed by the `side_effect_classification` CHECK constraint: `UNKNOWN` (default), `IDEMPOTENT`, `HAS_SIDE_EFFECTS`. No other values are accepted by the database.

| `side_effect_classification` | Crash detected? | Auto-reclaimed? | Replay safe? | Required action |
|---|---|---|---|---|
| `UNKNOWN` (default) | Yes | **No** | Unknown | Operator must explicitly classify |
| `IDEMPOTENT` | Yes | Yes (within retry limit) | Yes (idempotent adapters) | None — auto-reclaim permitted |
| `HAS_SIDE_EFFECTS` | Yes | **No** | Unknown | Operator must manually assess and reset |

## Checkpoint and Retry Behavior on Reclamation

| Scenario | Behavior |
|---|---|
| Reclaim with existing `checkpoint_ref` | `checkpoint_ref = NULL`; execution starts from scratch |
| No existing checkpoint | Starts from scratch (same as current behavior) |
| Adapter supports resume | Adapter reads its own state from external store |
| `retry_count >= max_retries`, classification `IDEMPOTENT` | Task → `failed`, `exhaustion_reason = 'retry_limit_exhausted'` |
| `retry_count >= max_retries`, classification `UNKNOWN` | Task → `review`, `exhaustion_reason = 'retry_limit_exhausted'` (operator handoff) |
| `retry_count >= max_retries`, classification `HAS_SIDE_EFFECTS` | Task → `review`, `exhaustion_reason = 'retry_limit_exhausted'` (operator handoff) |
| `retry_count < max_retries` on detect | `retry_count += 1`; reclaim proceeds |

## Open Trade-offs

### 1. TTL Duration

15 minutes is a starting proposal, not a proven default. Too short: slow workers (GC, I/O) reclaimed prematurely. Too long: genuine crashes go undetected for >15 minutes.

Per-profile TTL override via `WorkerProfile.lease_ttl` is the mechanism for adjusting for known slow adapters.

### 2. Heartbeat Failure Behavior

After a `StaleWorkerError` on heartbeat, the worker has lost ownership. Two options:

- **Stop immediately**: raise `StaleWorkerError` to the execution loop, discard result, stop.
- **Continue execution**: complete the adapter's `execute()`, then fail on the fenced `complete()`.

Recommendation: **stop immediately**. Continuing wastes resources on a fenced task and risks a partially-completed adapter execution with no way to commit its result.

### 3. Default `side_effect_classification`

The default `'UNKNOWN'` is a safety default — unclassified tasks are never auto-reclaimed regardless of whether they would be safe. Task authors must explicitly set `side_effect_classification = 'IDEMPOTENT'` to enable auto-reclaim, or `'HAS_SIDE_EFFECTS'` to require operator-managed recovery.

Risk: authors forget to classify. Mitigation: task creation API enforces that a value is provided; leaving the default is not permitted for tasks that will be queued for execution. Profile-level default of `'UNKNOWN'` with explicit override required at task creation.

### 4. Stale Worker Behavior After `StaleWorkerError`

| Option | Description |
|---|---|
| Silent discard | Worker drops the result, logs a warning |
| Emit `task.fenced` event | Worker emits a `task.fenced` event for observability, then discards |

Recommendation: silent discard + `task.fenced` event. Compensating transactions are application-specific and cannot be generalized in the kernel.

## Acceptance Test Matrix

| # | Scenario | Guarantee | Requires v0.2 impl |
|---|---|---|---|
| 1 | Worker crash is eventually detectable | Orphan detection query returns stale tasks | Yes (lock_expires_at + detection query) |
| 2 | Healthy slow worker is not reclaimed prematurely | `lock_expires_at` not yet passed | Yes (TTL + heartbeat) |
| 3 | Stale worker cannot overwrite newer result (complete) | Generation fence on `complete()` | Yes |
| 4 | Stale worker cannot overwrite newer result (fail) | Generation fence on `fail()` | Yes |
| 5 | Stale worker cannot refresh heartbeat after ownership transfer | Generation fence on heartbeat | Yes |
| 6 | Stale worker cannot write checkpoint after ownership transfer | Generation fence on `checkpoint_ref` update | Yes |
| 7 | Two recovery workers cannot both acquire ownership | Atomic reclaim — exactly one winner | Yes |
| 8 | Checkpoint is cleared on reclaim; recovery starts from scratch | `checkpoint_ref = NULL` on reclaim | Yes |
| 9 | `max_retries` with `IDEMPOTENT` task transitions to `failed` | Retry-exhaustion UPDATE sets `status='failed'`, `exhaustion_reason='retry_limit_exhausted'` | Yes |
| 10 | `UNKNOWN` task blocks auto-reclaim; on retry exhaustion, routes to `review` (no silent discard) | Reclaim predicate requires `= 'IDEMPOTENT'`; retry-exhaustion CASE routes `UNKNOWN` → `review` | Yes |
| 11 | `IDEMPOTENT` task permits auto-reclaim | Reclaim succeeds, `generation` and `retry_count` incremented | Yes |
| 12 | `HAS_SIDE_EFFECTS` task blocks auto-reclaim; on retry exhaustion, routes to `review` (no silent discard) | Reclaim predicate requires `= 'IDEMPOTENT'`; retry-exhaustion CASE routes `HAS_SIDE_EFFECTS` → `review` | Yes |
| 13 | Operator-managed recovery remains possible (manual reset to `queued`) | Manual reset SQL works; task becomes `next_runnable()` again | No (already works in v0.1; v0.2 adds fencing) |
| 14 | Operator reset increments generation; stale writes remain fenced | `generation` incremented on manual reset; stale worker's fenced write fails | Yes |
| 15 | A fenced heartbeat raises `StaleWorkerError` | Heartbeat UPDATE returns 0 rows after reclaim | Yes |

## Non-Goals (Out of Scope)

- No new task statuses beyond the 8 defined in ADR 0008. The `review` status is reused for operator-handoff tasks but is not new in v0.1.
- No distributed multi-orchestrator claiming (single orchestrator assumed).
- No automatic adapter-level retry (separate from crash recovery).
- No transactional outbox for external side effects.
- No Redis or external coordination service.
- No per-task TTL override in v0.2 (per-profile only).
