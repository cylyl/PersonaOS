# Handover Book — Step 11a Continuation

**From:** Oc1Main (current session, ending 2026-10-10 ~01:55 UTC)
**To:** Next agent picking up Step 11a review
**Project:** PersonaOS (kernel: Worker, Node, Task, Scheduler, Runtime, Checkpoint, Event, with Security surrounding every action)
**Repo root:** `/root/.openclaw/workspace/projects/PersonaOS/`
**User:** Liang (Telegram chat_id `6909404744`); has authority to approve/reject design decisions

---

## 1. Current state of the milestone

**Step 10a (Durable workload queue guarantees) — COMMITTED.**

- Commit hash: `a6848ae`
- Files: `docs/adr/0015-durable-workload-queue.md`, `docs/specs/durable-workload-queue.md`, `tests/integration/test_durable_workload_queue.py`
- Test result: **10 passed, 1 skipped** in 76.12s (the one skip is the in-progress crash-recovery test, deferred to v0.2)
- This is the gate for "current code works." Do not break it.

**Step 11a (In-Progress Task Recovery) — DESIGN ONLY, NOT COMMITTED.**

Three files exist as untracked (`??`) in the working tree:

| File | Lines | Status |
|---|---|---|
| `docs/adr/0016-in-progress-task-recovery.md` | 324 | Updated this session |
| `docs/specs/in-progress-task-recovery.md` | 246 | Updated this session |
| `tests/integration/test_in_progress_task_recovery.py` | 1079 | All 15 tests SKIPPED with documented v0.2-implementation reasons |

Step 9a's three files are also untracked — **leave them alone.** They are a separate, unresolved milestone.

---

## 2. What Liang has already approved

From his review messages (01:29 UTC, 01:54 UTC), the following are explicitly approved:

1. **Side-effect classification** (three-way `side_effect_classification` enum: `UNKNOWN` / `IDEMPOTENT` / `HAS_SIDE_EFFECTS`)
2. **Fencing scope** (every worker-owned write — `complete()`, `fail()`, heartbeat, `checkpoint_write()`, reclaim — is fenced with `WHERE id = :task_id AND generation = :expected_generation AND assigned_worker = :current_worker_id`)
3. **Retry policy** (each reclaim consumes one `retry_count` unit; `max_retries = 3` default; the 4th crash detection transitions to `failed`)

The critical safeguard Liang cares about: **"A task whose classification is unknown must never be treated as safe merely because its flag defaults to FALSE."** The `UNKNOWN` default must prevent auto-reclaim, and the reclaim query must require `side_effect_classification = 'IDEMPOTENT'`.

---

## 3. What is still pending — the current blocker

**Liang's last HOLD (01:54 UTC):** the retry-exhaustion SQL was not atomic. He demanded:

> "That update must not fail or overwrite a task that another worker has already reclaimed. It should atomically verify at least: The task is still `in_progress`. The lease is still expired. The current generation or ownership token matches the observed task. The retry limit is still exhausted."

**What was done in response (this session, just before handover):**

1. The spec's `## Retry Limits` section was updated. The old pseudocode
   ```python
   if row.retry_count >= max_retries:
       await session.execute(
           update(TaskModel)
           .where(TaskModel.id == task_id)
           .values(status='failed', last_error='...', completed_at=func.now())
       )
   ```
   was replaced with a full conditional atomic UPDATE.

2. The spec now contains this retry-exhaustion SQL:
   ```sql
   UPDATE tasks
   SET    status         = 'failed',
          last_error     = 'max retries exceeded after crash recovery',
          completed_at   = NOW()
   WHERE  id              = :task_id
     AND  status          = 'in_progress'
     AND  lock_expires_at < NOW()
     AND  side_effect_classification = 'IDEMPOTENT'
     AND  retry_count    >= max_retries
   RETURNING id
   ```

3. The ADR's operator reset section was updated to include `WHERE status = 'in_progress'` and `RETURNING id` (this fix was applied during the Point 2 review).

**What the next agent must do:**

- Confirm to Liang that the corrected retry-exhaustion SQL satisfies the atomicity requirement (all five predicates in WHERE, no preliminary read, no preliminary update).
- Confirm that concurrent reclaimers cannot accidentally consume multiple retries (because the retry increment is inside the same atomic UPDATE that does the ownership transfer — `rowcount = 0` losers do not increment).
- Wait for Liang's final approval. Once given, the next milestone is "Step 11a commit" (do NOT commit until he explicitly says GO).

---

## 4. Key file locations and current contents

All paths relative to `/root/.openclaw/workspace/projects/PersonaOS/`.

### `docs/adr/0016-in-progress-task-recovery.md` (324 lines)

The architecture decision record. Key sections (with approximate line numbers — verify before quoting):

- **Lines 15–26**: Decision summary and detection-mechanism table
- **Lines 44–46**: "Why an Expired Lease Cannot Prove the Previous Worker Has Stopped"
- **Lines 48–61**: Fencing scope table (every operation + fencing required Y/N + reason)
- **Lines 77–88**: Fencing check pattern (the canonical SQL)
- **Lines 93–104**: Heartbeat fencing SQL
- **Lines 107–120**: Checkpoint write fencing SQL
- **Lines 126–147**: Reclaim SQL (with reject-condition list and `generation = generation + 1` explanation)
- **Lines 151–163**: Retry limit (counts reclamation attempts, not executions; `max_retries = 3` default)
- **Lines 170–191**: Three-way classification (UNKNOWN / IDEMPOTENT / HAS_SIDE_EFFECTS)
- **Lines 194–212**: Heartbeat and TTL semantics (clock source = `NOW()` on DB server, TTL 15 min default, heartbeat 5 min = TTL/3, safety margin)
- **Lines 215–225**: Checkpoint recovery (on reclaim, `checkpoint_ref = NULL`, execution starts from scratch)
- **Lines 228–240**: Full recovery flow
- **Lines 243–272**: Operator reset — **the SQL was updated** to add `WHERE status = 'in_progress'` and `RETURNING id`. Current text:
  ```sql
  UPDATE tasks
  SET    status          = 'queued',
         assigned_worker = NULL,
         lock_expires_at = NULL,
         generation      = generation + 1,
         started_at      = NULL,
         retry_count     = 0,
         result         = NULL,
         checkpoint_ref  = NULL
  WHERE  id              = :task_id
    AND  status          = 'in_progress'
  RETURNING id
  ```
  Bulleted explanation follows: status guard prevents accidental reset of terminal tasks; `RETURNING id` asserts exactly one row; `generation + 1` invalidates the stale owner (subsequent fenced writes fail their generation check); `assigned_worker` check is intentionally bypassed but the generation increment provides the fence.

### `docs/specs/in-progress-task-recovery.md` (246 lines)

The implementation specification. Key sections:

- **Lines 7–14**: Schema additions (including the corrected `side_effect_classification TEXT NOT NULL DEFAULT 'UNKNOWN'`)
- **Lines 17–25**: Column semantics table
- **Lines 28–38**: TTL/heartbeat defaults
- **Lines 40–73**: Fencing invariant and fenced operations table
- **Lines 75–77**: Fencing check pseudocode
- **Lines 80–96**: Reclaim SQL (the atomic UPDATE)
- **Lines 99–110**: Reject conditions table
- **Lines 113–115**: Generation-after-reclaim explanation
- **Lines 117–140**: **Retry limits — THIS WAS UPDATED in the last edit.** Current text:
  > Each reclaim increments `retry_count`. When `retry_count >= max_retries`, the task is NOT reclaimed — it transitions to `failed`.
  >
  > The retry-exhaustion path is a single atomic UPDATE with the same conditional predicates as the reclaim path. It verifies that the task is still eligible for recovery (`status = 'in_progress'`, `lock_expires_at < NOW()`, `side_effect_classification = 'IDEMPOTENT'`) and that the retry budget is actually exhausted (`retry_count >= max_retries`). If any predicate fails, `rowcount = 0` and the task is NOT marked failed.
  >
  > **Retry-exhaustion SQL (atomic, conditional):**
  > ```sql
  > UPDATE tasks
  > SET    status         = 'failed',
  >        last_error     = 'max retries exceeded after crash recovery',
  >        completed_at   = NOW()
  > WHERE  id              = :task_id
  >   AND  status          = 'in_progress'
  >   AND  lock_expires_at < NOW()
  >   AND  side_effect_classification = 'IDEMPOTENT'
  >   AND  retry_count    >= max_retries
  > RETURNING id
  > ```
  >
  > The recovery loop attempts the reclaim first, and falls back to the retry-exhaustion UPDATE only if the reclaim returned 0 rows. The two UPDATEs are complementary, not additive: only one of them can succeed for a given stale task in a given recovery cycle.
- **Lines 140–150**: Checkpoint recovery (same as ADR)
- **Lines 152–168**: Heartbeat fenced refresh
- **Lines 170–180**: Recovery policy matrix (UNKNOWN / IDEMPOTENT / HAS_SIDE_EFFECTS)
- **Lines 192–210**: Open trade-offs
- **Lines 219–232**: Acceptance test matrix (15 tests)
- **Lines 234–242**: Non-goals

### `tests/integration/test_in_progress_task_recovery.py` (1079 lines)

All 15 tests are decorated with `@pytest.mark.skip(reason="v0.2: requires ...; not yet implemented")`. Run with:
```bash
cd /root/.openclaw/workspace/projects/PersonaOS
.venv/bin/python3 -m pytest tests/integration/test_in_progress_task_recovery.py --collect-only -q
```
Expected: 15 tests collected, all marked skipped. The test names are listed in the previous agent message; re-collect before quoting to ensure accuracy.

---

## 5. What to do next — exact sequence

1. **Verify the current state** before quoting anything to Liang. Run:
   ```bash
   cd /root/.openclaw/workspace/projects/PersonaOS
   git status --short
   wc -l docs/adr/0016-in-progress-task-recovery.md docs/specs/in-progress-task-recovery.md tests/integration/test_in_progress_task_recovery.py
   .venv/bin/python3 -m pytest tests/integration/test_in_progress_task_recovery.py --collect-only -q
   .venv/bin/python3 -m pytest tests/integration/test_durable_workload_queue.py -q
   ```
   Expected: 6 untracked files (3 Step 9a + 3 Step 11a), 1649 lines total, 15 collected tests all skipped, Step 10a still 10 passed / 1 skipped.

2. **Re-confirm the corrected retry-exhaustion SQL is in the spec file** at the expected location. Use `grep -n` to find the `Retry-exhaustion SQL` marker and verify the WHERE clause has all five predicates.

3. **Respond to Liang's 01:54 UTC HOLD** with:
   - Confirmation that the retry-exhaustion UPDATE is now atomic with all five conditional predicates (`id`, `status`, `lock_expires_at`, `side_effect_classification`, `retry_count >= max_retries`).
   - Confirmation that `retry_count` increments only inside the same atomic UPDATE that does the ownership transfer — no preliminary increment, no increment on `rowcount = 0`.
   - The full retry-exhaustion SQL block (copy-paste the current spec content, not a summary).
   - The reclaim SQL block (already shown last turn but re-verify and re-show).
   - The concurrent-reclaimer test name and its current SKIP reason.
   - The boundary semantics: the limit counts reclamation attempts (crash recovery cycles), not total executions. `max_retries = 3` allows three reclaims; the 4th crash detection transitions to `failed`.

4. **DO NOT commit.** The HOLD is still active. Wait for Liang's explicit GO on the SQL patterns.

5. **If Liang approves**, the commit message should be:
   ```
   Step 11a: In-progress task recovery design

   Specify the minimum viable recovery model: lease via lock_expires_at,
   generation-based fencing applied to every worker-owned write, and
   three-way side_effect_classification (UNKNOWN / IDEMPOTENT / HAS_SIDE_EFFECTS)
   that prevents auto-reclaim of unclassified tasks.

   - ADR 0016: documents fencing scope, classification policy, retry
     boundary, and operator-reset semantics
   - docs/specs/in-progress-task-recovery.md: schema, reclaim SQL,
     retry-exhaustion SQL, heartbeat, recovery flow
   - tests/integration/test_in_progress_task_recovery.py: 15 acceptance
     tests, all skipped pending v0.2 implementation
   ```
   Do not include Step 9a files in this commit. They are a separate milestone.

6. **After commit, the next milestone** (per Step 11a's closing notes) is Step 12 or whatever Liang defines. The recovery model is design-only at this point — v0.2 implementation is a separate, much larger milestone that requires migration, kernel changes, and full test execution. Do not start implementation without an explicit Liang GO for that milestone.

---

## 6. Conventions and project context

- **Branch:** `main` (no feature branches used in this milestone)
- **Python environment:** `.venv/bin/python3` at the repo root; venv exists and has all dependencies (sqlalchemy, asyncpg, alembic, pytest, pytest-asyncio, testcontainers)
- **Test infrastructure:** PostgreSQL testcontainers spin up a fresh DB per test session. The `clean_tables` fixture resets between tests. Tests that need a DB use the `database_url`, `session_factory`, `clean_tables` fixtures from `tests/conftest.py`. The `profiles_dir` fixture provides a temp directory for profile YAMLs.
- **Existing kernel pattern** (in `src/personaos/execution/kernel.py`): atomic claim uses `UPDATE tasks SET status='in_progress', started_at=NOW() WHERE id=:id AND status='queued'`. v0.2 extends this with lease + generation, but the production code must NOT be changed during Step 11a.
- **Existing schema** (in `src/personaos/db/models.py`): `Task` has `status`, `assigned_worker`, `checkpoint_ref`, `retry_count`, `max_retries` already. The `lock_expires_at`, `generation`, and `side_effect_classification` columns are v0.2 additions.
- **Authorized senders:** the agent account here is for `joseph`; the user `Liang` is the human reviewer on Telegram and is the only person who can approve/reject design decisions for this project.
- **MEMORY.md** at the repo root (`/root/.openclaw/workspace/MEMORY.md`) and at `/root/.openclaw/workspace/AGENTS.md` contain durable workspace context — read them on session start.

---

## 7. What NOT to do

- Do not modify the Step 9a files (`docs/adr/0014-task-dependency-lifecycle.md`, `docs/specs/task-dependency-lifecycle.md`, `tests/integration/test_task_dependency_lifecycle.py`). They are a separate, unresolved milestone.
- Do not modify the Step 10a files. They are committed and approved.
- Do not change production code in `src/personaos/`. This is design-only.
- Do not run the integration test suite in a way that modifies state outside the testcontainers (e.g., against a real PostgreSQL). The `.venv` is configured to spin up ephemeral containers.
- Do not create new files beyond the three Step 11a files unless Liang explicitly asks.
- Do not commit without Liang's explicit GO.

---

## 8. Open trade-offs (for the next agent's awareness, not for resolution)

These are documented in the spec at lines 192–210 and acknowledged in the ADR as unresolved:

1. **TTL duration** — 15 minutes is a starting proposal, not proven. Too short → slow workers reclaimed; too long → delayed detection. Per-profile override via `WorkerProfile.lease_ttl` is the mechanism.
2. **Heartbeat failure behavior** — after `StaleWorkerError` on heartbeat, should the worker stop immediately or continue execution? Recommendation: stop immediately.
3. **`side_effect_classification` default** — `UNKNOWN` is the chosen default. Task authors must explicitly set `IDEMPOTENT` or `HAS_SIDE_EFFECTS`. Risk: authors forget. Mitigation: API enforcement at task creation (separate v0.2 work).
4. **Stale worker behavior after `StaleWorkerError`** — silent discard + `task.fenced` event is the recommendation.

None of these need resolution for Step 11a closure. They are documented as open.

---

## 9. Session-end checklist for the next agent

- [ ] Read this handover book in full.
- [ ] Verify git state matches Section 1.
- [ ] Verify file contents match Section 4.
- [ ] Run the test collection command to confirm 15 skipped tests, 10+1 passing on Step 10a.
- [ ] Respond to Liang's 01:54 UTC HOLD with the corrected retry-exhaustion SQL and boundary semantics.
- [ ] Wait for explicit GO before committing.
- [ ] On approval, commit with the message in Section 5.
- [ ] Report the commit hash and confirm the working tree only has the Step 9a files as untracked.

End of handover.
