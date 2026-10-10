# ADR 0016 — In-Progress Task Recovery

**Date:** 2026-10-09
**Status:** Draft
**Deciders:** PersonaOS core team

## Context

ADR 0015 established that in-progress tasks are NOT automatically recovered in v0.1. An executor crash leaves the task in `in_progress` indefinitely, requiring operator intervention. This ADR addresses the next question: what would a safe, automatic recovery mechanism look like, and what trade-offs does it carry?

This is a design-only ADR. No implementation until reviewed.

## Decision

**The minimum viable recovery model: lease via `lock_expires_at` + per-heartbeat TTL extension + orphan detection + `generation`-based fencing applied to every worker-owned write.**

## Detection: Lease vs Heartbeat vs TTL

| Mechanism | Detects crash? | Distinguishes slow from dead? | Complexity |
|---|---|---|---|
| TTL only | Late (only at absolute expiry) | No | Low |
| Heartbeat only | Yes (gap in heartbeats) | No (gap could be slow worker) | Medium |
| Lease (TTL + heartbeat) | Yes (expiry after heartbeat gap) | Partially (generous TTL needed) | Medium |
| Distributed lease (Redis) | Yes | Yes | High |

**Recommendation for v0.2: Lease via `lock_expires_at` + `generation`. PostgreSQL-only, no external coordination service.**

## Why an Expired Lease Cannot Prove the Previous Worker Has Stopped

This is the central safety property.

An expired lease means: *"the system believes this worker is no longer alive."* It does NOT mean: *"the worker has stopped."*

The worker could be:
- Slow but alive (GC pause, I/O stall, network partition)
- Alive and still processing (TTL was set too aggressively)
- Alive and about to complete and write the result

Auto-resetting `in_progress → queued` based solely on an expired lease causes:
- **Duplicate execution**: stale and new worker both complete the same task
- **Corrupt results**: stale worker writes `completed` after new worker already wrote
- **Side-effect duplication**: non-idempotent external actions execute twice

**Lease expiry alone is insufficient. Fencing is required.**

## Fencing

Fencing prevents a stale worker from writing any state to a task that has been reclaimed.

### Scope: Every Worker-Owned Write Must Be Fenced

Fencing is not limited to `complete()`. Every state-changing operation by the worker must validate ownership and generation:

| Operation | Fencing required? | Reason |
|---|---|---|
| `complete()` (write result + `completed`) | **Yes** | Prevent stale result overwriting newer result |
| `fail()` (write `last_error` + `failed`) | **Yes** | Prevent stale failure overwriting newer result |
| `heartbeat` (refresh `lock_expires_at`) | **Yes** | Prevent stale worker from keeping lease alive after ownership transferred |
| `checkpoint_write()` (write `task_checkpoints` + update `checkpoint_ref`) | **Yes** | Prevent stale worker from overwriting newer checkpoint with stale state |
| Claim / reclaim (set `lock_expires_at` + increment `generation`) | **Yes** | Atomic ownership transfer; two workers must not both win |
| Operator reset (clear lease, set `queued`) | **No** | Operator bypass is explicit and intentional; fencing handled by kernel-level guard |

### Mechanism: `generation` Column

`generation` is a monotonically increasing integer on every task. It starts at `0` on task creation and increments on every claim or reclaim.

```
Worker A claims:    generation = 1
Worker A crashes
Worker B reclaims:   generation = 2
Worker A recovers, tries to write checkpoint, result, or heartbeat
  → generation check fails (expects 2, finds 1) → rejected
```

### Fencing Check Pattern

Every worker-owned write uses this pattern:

```sql
UPDATE tasks
SET    <fields> = <new_values>,
       generation = generation  -- unchanged; used as match key only
WHERE  id = :task_id
  AND  generation = :expected_generation   -- fenced: fails if stale
  AND  assigned_worker = :current_worker_id -- fenced: fails if ownership transferred
```

If `rowcount == 0`: task has been reclaimed → raise `StaleWorkerError`, discard write.

The `assigned_worker = :current_worker_id` clause is the ownership check. The `generation = :expected_generation` clause is the generation fence. Both are required.

### Heartbeat Fencing

The heartbeat is the most frequent write. Its fencing check:

```sql
UPDATE tasks
SET    lock_expires_at = NOW() + :lease_ttl
WHERE  id = :task_id
  AND  generation = :expected_generation
  AND  assigned_worker = :current_worker_id
```

If `rowcount == 0`: the task has been reclaimed → stop heartbeating, raise `StaleWorkerError`.

### Checkpoint Write Fencing

When a worker writes a checkpoint during execution:

```sql
UPDATE tasks
SET    checkpoint_ref = :new_ref
WHERE  id = :task_id
  AND  generation = :expected_generation
  AND  assigned_worker = :current_worker_id
```

The `task_checkpoints` row is inserted first (no FK conflict). The `tasks.checkpoint_ref` update is what gets fenced.

**Design note**: The stale worker's checkpoint writes are wasted, but they cannot overwrite the new owner's checkpoint because the update is fenced. The new owner must also fence its checkpoint writes — it has the current generation, so it succeeds.

## Orphan Detection + Ownership Transfer: Atomic Reclaim Transaction

Two recovery workers must not both acquire the same task. The orphan detection query and the ownership transfer must be combined in a single atomic SQL statement.

### Reclaim SQL

```sql
UPDATE tasks
SET    status          = 'in_progress',
       assigned_worker  = :new_worker_id,
       lock_expires_at = NOW() + :lease_ttl,
       generation      = generation + 1,
       -- Clear checkpoint so recovery execution starts fresh
       -- (or keep it — see §Checkpoint Recovery below)
       checkpoint_ref  = NULL,
       started_at      = NOW()
WHERE  id              = :task_id
  AND  status          = 'in_progress'
  AND  generation      = :observed_generation      -- fenced: fails if a recovery-loop
                                                    -- write raced after the read
  AND  lock_expires_at < NOW()
  AND  side_effect_classification = 'IDEMPOTENT'
  AND  retry_count    < max_retries
RETURNING id, generation
```

- The `WHERE` clause is the atomic claim condition. Two concurrent reclaims: one wins (rowcount=1), one loses (rowcount=0, `TaskStatusError`).
- `generation = :observed_generation` is the recovery-loop fence. The recovery worker reads the task (observing `generation = N`), then runs the reclaim UPDATE. If a concurrent operator reset or another recovery operation changed the generation between the read and the UPDATE, the WHERE fails (0 rows).
- `generation + 1` increments the fence. The new owner holds `generation = N+1`. The stale owner holds `generation = N` and is fenced out of all subsequent writes.
- `retry_count < max_retries` enforces the retry limit at reclaim time.
- `side_effect_classification = 'IDEMPOTENT'` gates automatic reclamation on side-effect risk. Tasks with `UNKNOWN` or `HAS_SIDE_EFFECTS` are never auto-reclaimed.

The two reclaimer predicates on `retry_count` (`< max_retries`) and the retry-exhaustion predicate (`>= max_retries`) are **mutually exclusive**. A single recovery worker that lands first wins; the other sees zero rows.

## Retry Limit

Each reclaim consumes one unit of the task's retry budget:

```
retry_count: number of times this task has been reclaimed after a crash
max_retries: configured upper limit (default: 3, configurable per profile)
```

`max_retries = 0` is permitted and **disables automatic reclamation**: such tasks must be reset by an operator; the kernel will not auto-reclaim them.

On reclaim:
- If `retry_count >= max_retries`: the reclaim predicate fails (0 rows affected). The recovery loop falls through to the retry-exhaustion path described below.
- If `retry_count < max_retries`: increment `retry_count`, proceed with reclaim.

**This is distinct from adapter-level retry** (which applies when an adapter fails during execution). Crash-recovery retries apply when the *worker* crashes, not when the *adapter* reports failure.

**Precise definition**: The existing `retry_count` and `max_retries` fields already exist in the schema (ADR 0008). v0.2 reuses them for crash-recovery reclaim counting, extending their semantics. This is a natural fit: `retry_count` tracks how many times execution has been *attempted* — whether due to adapter failure or crash recovery.

## Retry Exhaustion: Classification-Aware Termination

When the reclaim predicate fails because `retry_count >= max_retries`, the recovery loop must still transition the task out of `in_progress`. **It must not silently leave the task in `in_progress` forever, and it must not silently mark every exhausted task as `failed`.** The correct destination depends on `side_effect_classification`.

### Retry-Exhaustion SQL (atomic, classification-aware)

```sql
UPDATE tasks
SET    status            = CASE
           WHEN side_effect_classification = 'IDEMPOTENT'   THEN 'failed'
           WHEN side_effect_classification = 'UNKNOWN'      THEN 'review'
           WHEN side_effect_classification = 'HAS_SIDE_EFFECTS' THEN 'review'
         END,
       last_error        = COALESCE(last_error, 'max retries exceeded after crash recovery'),
       exhaustion_reason  = 'retry_limit_exhausted',
       completed_at       = CASE
           WHEN side_effect_classification = 'IDEMPOTENT'   THEN NOW()
           ELSE NULL  -- 'review' tasks are not yet completed
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

| Classification | Destination status | Operator-visible via | Why |
|---|---|---|---|
| `IDEMPOTENT` | `failed` (terminal) | `tasks.status = 'failed' AND exhaustion_reason = 'retry_limit_exhausted'` | Re-running is provably safe; caller may retry from scratch |
| `UNKNOWN` (default) | `review` (operator handoff) | `tasks.status = 'review'` | Re-running is *not* known to be safe; operator must classify and reset |
| `HAS_SIDE_EFFECTS` | `review` (operator handoff) | `tasks.status = 'review'` | Re-running is *known* unsafe; operator must assess partial state and reset |

**The `review` status is part of the v0.1 status enum** (`'queued', 'assigned', 'in_progress', 'blocked', 'review', 'completed', 'failed', 'canceled'`, ADR 0008 / migration 0001). No new status value is added; the v0.1 enum is leveraged as the operator-handoff state. This avoids a schema migration for the status type.

**The `exhaustion_reason` column is new** (see Schema Additions below). It is distinct from `last_error` (which is set on every failure path) and gives the operator UI an unambiguous signal that this is a crash-recovery retry-exhaustion, not an adapter-reported failure.

**Why this is not a silent discard**: every retry-exhausted task ends in either `failed` (caller can drop, was idempotent) or `review` (operator can see, was non-idempotent). No task is dropped into a non-queryable state. The operator's only required follow-up is the `review` queue.

### Mutually-Exclusive Eligibility Between Reclaim and Retry-Exhaustion

| Path | `retry_count` predicate | `side_effect_classification` predicate | `generation` predicate |
|---|---|---|---|
| Reclaim | `retry_count < max_retries` | `= 'IDEMPOTENT'` | `= :observed_generation` |
| Retry-exhaustion | `retry_count >= max_retries` | `IN ('IDEMPOTENT', 'UNKNOWN', 'HAS_SIDE_EFFECTS')` | `= :observed_generation` |

The two paths partition the stale-task set on `retry_count` and `status`. After either path runs, the row is no longer `in_progress` (or no longer stale), so the other path sees zero rows. There is no scenario where both affect the same task in the same recovery cycle.

Both paths share the same `generation = :observed_generation` fence. A recovery worker's decision is computed from a task read at a specific generation; if that generation is no longer current, the worker's UPDATE is a no-op. This prevents stale recovery-loop decisions from terminating or reclaiming tasks whose ownership has changed since the decision was made.

### Fencing on Retry Exhaustion

The retry-exhaustion UPDATE **does include `generation = :observed_generation` in the WHERE clause**. The recovery worker reads the task (observing the current `generation`, `retry_count`, `lock_expires_at`, `side_effect_classification`) and then runs the UPDATE. If any of these observed values changes between the read and the UPDATE — for example, because an operator reset incremented the generation and cleared `retry_count`, or because a concurrent recovery operation re-claimed the task — the WHERE clause fails and the UPDATE affects 0 rows.

**Stale-decision scenario**: recovery worker A reads task at `generation=N`, `retry_count=max_retries`, decides retry-exhaustion is appropriate. Before A's UPDATE lands, operator B resets the task (status→queued, retry_count→0, generation→N+1). Then worker C re-claims (status→in_progress, generation→N+2, retry_count=1). Worker A's retry-exhaustion UPDATE: `generation = N` no longer matches (now N+2), 0 rows. Correctly no-op.

**Without the generation fence**, A's UPDATE would still match on `status='in_progress'` and `retry_count>=max_retries` (if a third crash happened to set retry_count back to max_retries). The generation fence eliminates this ambiguity.

The status and `lock_expires_at` predicates remain in the WHERE clause as additional defenses (a worker that has just reclaimed or an operator who is mid-reset cannot have its row hijacked by an in-flight retry-exhaustion). PostgreSQL's row-level lock + WHERE re-evaluation after lock wait provides the same guarantee as in the reclaim path.

## Idempotency and External Side Effects

Fencing prevents **state corruption** but cannot undo **external side effects**.

If a task sent an email, charged a credit card, or wrote to an external API, the stale worker may have already performed those actions before being fenced. No mechanism can reliably roll them back within the kernel.

### Classification: Three-Way `side_effect_classification`

Every task must be explicitly classified before it can be auto-reclaimed. A task is never auto-reclaimed based on an implicit assumption.

```sql
ALTER TABLE tasks ADD COLUMN side_effect_classification TEXT NOT NULL DEFAULT 'UNKNOWN';
```

| Value | Meaning | Auto-reclaim? |
|---|---|---|
| `'UNKNOWN'` (default) | Side-effect behavior has not been explicitly classified. | **No** — operator must review |
| `'IDEMPOTENT'` | Task author has confirmed no non-idempotent external side effects. | **Yes**, subject to fencing and retry limits |
| `'HAS_SIDE_EFFECTS'` | Task author has confirmed non-idempotent external side effects exist. | **No** — operator must manually reset |

**The default `'UNKNOWN'` is a safety default.** It is not equivalent to "safe to auto-reclaim." A task whose classification is unknown must never be treated as idempotent. The system does not auto-detect side effects.

**How classification is set**: Task authors set `side_effect_classification = 'IDEMPOTENT'` or `'HAS_SIDE_EFFECTS'` at task creation time (or before first execution). If left at `'UNKNOWN'`, the task can still be executed normally but will never be auto-reclaimed after a crash — it requires operator review.

**Why not boolean**: A boolean defaulting to `FALSE` is indistinguishable from an intentionally confirmed-safe task. The three-way classification makes the unknown state explicit and prevents silent auto-reclaim of unclassified tasks.

### Approaches for External Side Effects and Their Limits

| Approach | Works for? | Limit |
|---|---|---|
| Idempotent operations (e.g. "send if not sent") | Email, notifications, API calls | Requires external system support |
| At-least-once + deduplication | Message queues | Requires deduplication key design |
| Compensating transactions | Financial operations | Complex, not universal |
| Operator-managed replay | Any side-effect task | Human must assess |

## Heartbeat and TTL Semantics

### Clock Source

All time comparisons use **database server time** (`NOW()` / `func.now()`), not worker-local clock. This prevents clock skew between workers and the database from causing premature or delayed orphan detection.

### TTL Default

- Default lease TTL: **15 minutes**
- Rationale: accommodates slow adapters (file I/O, network calls) without excessively delaying crash detection
- Per-profile override via `WorkerProfile.lease_ttl` (optional field, nullable → inherits default)

### Heartbeat

- Heartbeat interval: `TTL / 3` = **5 minutes** (with 15-minute TTL)
- Heartbeat is **not required for liveness** — a worker that stops heartbeating is not declared dead until `lock_expires_at` passes.
- Heartbeat is **best-effort**: a missed heartbeat does not trigger orphan detection. Orphan detection runs on `lock_expires_at` only.
- If a heartbeat's fenced UPDATE returns 0 rows (`StaleWorkerError`): the worker must stop immediately, not retry.

### Safety Margin

Orphan detection runs periodically (default: every 30 seconds). The effective detection latency is: `lock_expires_at + detection_interval`.

With a 15-minute TTL and 30-second detection interval: worst-case detection = **15 minutes 30 seconds**.

A slow-but-alive worker with a 20-minute execution time would be incorrectly reclaimed with a 15-minute TTL. Mitigation: profiles with known long-running adapters must set a longer TTL via `WorkerProfile.lease_ttl`.

## Checkpoint Recovery on Reclamation

When a task is reclaimed:

1. `checkpoint_ref` is **set to `NULL`** during reclaim. This is intentional.
2. Recovery execution starts **from the beginning** of the adapter's `execute()` method.
3. The adapter is responsible for reading its own checkpoint state if it supports resume. This means: the adapter must persist its own checkpoint (not kernel-level) if it needs resume capability. The kernel-level `checkpoint_ref` is cleared to avoid a stale adapter reading a pre-crash checkpoint.

**Rationale**: The kernel cannot safely resume from a checkpoint that was written by a stale worker before the crash. The stale worker's checkpoint may represent partial side effects that are now invalid. Starting fresh avoids this risk.

**Alternative considered**: Keep `checkpoint_ref` on reclaim, let the adapter choose whether to use it. Rejected because the safety risk of a stale adapter checkpoint is not visible to the kernel — the adapter might silently use the wrong checkpoint state.

## Recovery Flow (Full)

1. **Orphan detection** finds tasks where `lock_expires_at < NOW()`, `side_effect_classification = 'IDEMPOTENT'`, `retry_count < max_retries`.
2. **Atomic reclaim**: `UPDATE ... WHERE lock_expires_at < NOW()` — exactly one worker wins.
3. New owner holds `generation = N+1`. `checkpoint_ref = NULL`, `retry_count += 1`.
4. Recovery execution runs from scratch (no kernel-level checkpoint). Adapter may maintain its own resume state.
5. On `complete()` / `fail()`: fenced UPDATE with `generation = N+1` succeeds.
6. Stale worker (holding `generation = N`) attempts any write → fenced → `StaleWorkerError` → discarded.

If `retry_count >= max_retries` at step 1: the reclaim predicate fails. The recovery loop runs the **retry-exhaustion** UPDATE instead, which transitions the task to:
- `failed` (terminal) if `side_effect_classification = 'IDEMPOTENT'`, or
- `review` (operator handoff) if `side_effect_classification` is `UNKNOWN` or `HAS_SIDE_EFFECTS`.

The two paths are mutually exclusive on `retry_count`; only one of them succeeds per stale task per recovery cycle.

## Operator Reset

An operator can always force-reset a task:

```sql
UPDATE tasks
SET    status          = 'queued',
       assigned_worker = NULL,
       lock_expires_at = NULL,
       generation      = generation + 1,
       started_at      = NULL,
       retry_count     = 0,  -- reset retry budget on manual reset
       result         = NULL,
       checkpoint_ref  = NULL
WHERE  id              = :task_id
  AND  status          = 'in_progress'   -- only non-terminal tasks
RETURNING id
```

- **`WHERE status = 'in_progress'`** is the status guard: the operator reset can only target a task that is currently in `in_progress`. It cannot accidentally reset a `completed` or `failed` task.
- **`RETURNING id`** confirms exactly one row was updated. If rowcount is 0, the task was already terminal — the reset is a no-op.
- **`generation = generation + 1`** invalidates the stale owner: after reset, the stale worker holds an outdated generation and every subsequent fenced write fails its `generation = :expected` check.
- This SQL intentionally bypasses the `assigned_worker` check (operator identity is not tracked), but the incremented `generation` ensures the stale owner is fenced out regardless.

## Compatibility

- The lifecycle remains `queued → in_progress → completed/failed`. The `review` status (already present in the v0.1 status enum) is now reachable from `in_progress` via the retry-exhaustion path for non-idempotent tasks. No new status value is added.
- Kernel remains sole authority for all transitions.
- `completed` and `failed` remain terminal — no re-transition possible. `review` is non-terminal and is reachable from `in_progress` (retry-exhaustion) and from operator manual action; an operator transitions `review` back to `queued` or to `failed`.
- Existing `retry_count` / `max_retries` fields are reused for crash-recovery retry counting. `max_retries` is `NOT NULL` with server default `3`; `max_retries = 0` is permitted and disables auto-reclamation.
- Existing `last_error`, `started_at`, `completed_at`, `assigned_worker`, `checkpoint_ref` fields are reused as-is. `assigned_worker` is the canonical owner column (do not rename to `owner_id`).

### Schema Additions (v0.2)

```sql
-- 4 new columns. All additive. Existing columns untouched.
ALTER TABLE tasks ADD COLUMN lock_expires_at             TIMESTAMPTZ NULL;
ALTER TABLE tasks ADD COLUMN generation                INTEGER      NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN side_effect_classification TEXT        NOT NULL DEFAULT 'UNKNOWN'
  CHECK (side_effect_classification IN ('UNKNOWN', 'IDEMPOTENT', 'HAS_SIDE_EFFECTS'));
ALTER TABLE tasks ADD COLUMN exhaustion_reason          TEXT         NULL;

-- Replaces the free-form ad-hoc `requires_operator_replay` concept that
-- appeared in earlier drafts. The status enum already has 'review' for
-- operator-handoff tasks; `side_effect_classification` is the
-- per-task policy that decides *whether* a task routes there on retry
-- exhaustion.

CREATE INDEX ix_tasks_stale_in_progress
  ON tasks (lock_expires_at)
  WHERE status = 'in_progress' AND lock_expires_at IS NOT NULL;

CREATE INDEX ix_tasks_review_queue
  ON tasks (priority, created_at)
  WHERE status = 'review';
```

The `side_effect_classification` CHECK constraint enforces the canonical three-way vocabulary (`UNKNOWN` / `IDEMPOTENT` / `HAS_SIDE_EFFECTS`) at the database boundary. Any other value is rejected by Postgres regardless of caller code.

## Consequences

### Positive

- Crashed workers detected automatically without operator intervention.
- Fencing applied to all worker-owned writes prevents all result corruption by stale workers.
- Heartbeat fencing gives stale workers a definitive error, preventing wasted lock refresh after ownership transfer.
- Retry limit prevents infinite reclaim loops.
- The `side_effect_classification` policy (with `UNKNOWN` as the safe default) prevents silent auto-reclaim of unclassified tasks.
- Retry exhaustion is classification-aware: `IDEMPOTENT` → `failed`, `UNKNOWN` / `HAS_SIDE_EFFECTS` → `review`. No silent discard, no false "completed" on potentially-unsafe tasks.

### Negative

- Wasted work: a fenced slow worker has completed adapter execution for nothing.
- Non-idempotent tasks require operator involvement regardless of fencing.
- TTL tuning required: too short → slow workers reclaimed; too long → delayed detection.

### Open Trade-offs

1. **Who sets the TTL?** Default of 15 min vs per-profile override. No per-task override in v0.2.
2. **Heartbeat failure behavior?** After `StaleWorkerError` on heartbeat, worker should stop immediately or continue execution? Recommendation: stop — continuing wastes resources on a fenced task.
3. **Should operator reset reset `retry_count` to 0?** Yes — a manual reset indicates operator has assessed the situation and wants to give the task a fresh budget.
4. **`task.fenced` event?** Recommendation: yes, emitted when a worker receives `StaleWorkerError`, for observability.

## References

- ADR 0015 (durable workload queue): in-progress recovery out of scope v0.1
- ADR 0010 (worker execution kernel): lifecycle state machine
- ADR 0008 (worker profile versioning): v0.1 schema, `retry_count`/`max_retries` fields
