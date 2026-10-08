# ADR 0012 — Worker Orchestrator

**Status:** Proposed (2026-10-08)
**Scope:** PersonaOS v0.1 extension (closes the loop on Steps 3-6)
**Supplements:** [ADR 0008 — Thin Worker, six tables](0008-thin-worker-six-tables.md), [ADR 0009 — WorkerExecutionContext](0009-worker-execution-context.md), [ADR 0010 — WorkerExecutionKernel](0010-worker-execution-kernel.md)

## Context

Steps 3-6 built the execution plumbing:

- **Step 3** — WorkerProfile versioning + immutable snapshots on disk
- **Step 4** — WorkerExecutionContext (frozen bundle of worker + snapshot profile + task)
- **Step 5** — WorkerExecutionKernel (orchestrates task lifecycle; Adapter-agnostic)
- **Step 6** — Simulation/replay (re-run historical tasks via the same kernel + different Adapter)

But there is no persistent loop that drives the kernel. The kernel is invoked
manually — by a developer running `kernel.execute(task)` or by a test harness.
PersonaOS has no way to **run** in the background: find work, claim it, execute
it, repeat. Every existing test calls `kernel.execute(task)` synchronously.

This means PersonaOS today is a *library*, not a *service*.

Step 7 closes that gap with a `WorkerOrchestrator` — a small async polling
loop that finds queued tasks and drives the existing kernel.

## Decision

### 1. `WorkerOrchestrator` — `src/personaos/orchestrator/worker.py`

```python
class WorkerOrchestrator:
    def __init__(
        self,
        *,
        session: AsyncSession,
        kernel: WorkerExecutionKernel,
        poll_interval: float = 0.1,
    ) -> None: ...

    async def run(self) -> None:
        """Poll until stop() is called. Never returns unless stopped."""

    async def process_one(self) -> bool:
        """One iteration. Returns True if processed, False if no work."""

    def stop(self) -> None:
        """Request clean shutdown."""

    @property
    def is_running(self) -> bool: ...

    @property
    def metrics(self) -> "OrchestratorMetrics": ...
```

The orchestrator is the **single piece of polling logic**. It does not own
the lifecycle, the claim semantics, the context resolution, or the adapter.
Those all live in the kernel + registry from Steps 3-5. The orchestrator's
only job is **find + invoke + handle the concurrency boundary**.

### 2. Atomic claim — uses the existing kernel mechanism (Phase 3)

The orchestrator does NOT add a new claim API. It does not extend the task
state machine. The atomic claim already exists — it's the kernel's Phase 3
(`UPDATE tasks SET status='in_progress' WHERE id=? AND status='queued'`).

The orchestrator's `process_one()`:

```
SELECT id FROM tasks WHERE status='queued' ORDER BY created_at LIMIT 1
       ↓
Task.from_orm(row)
       ↓
kernel.execute(task)
       ↓
  kernel Phase 3: UPDATE WHERE status='queued'  ← the atomic claim
       ↓
  result.status = "completed" | TaskStatusError
```

The kernel's WHERE on `status='queued'` is what makes the claim atomic.
Two orchestrators that both call `kernel.execute(task)` on the same queued
task: one kernel's Phase 3 succeeds, the other's finds `rowcount=0` and
raises `TaskStatusError`. The orchestrator catches `TaskStatusError`,
increments `claim_conflicts`, and continues.

**No `claim_next()` method on WorkloadService. No new state machine. No leases.**

This is deliberate. Adding a parallel claim path would mean:
- The orchestrator sets `status='in_progress'` first, then the kernel's Phase 3
  fails (because status is no longer 'queued'), and we need to either:
  - (a) Modify the kernel to skip Phase 3 when called from the orchestrator, OR
  - (b) Add a new state like 'assigned' between 'queued' and 'in_progress'
- Either way, the kernel now has *two* paths to do the same thing, which
  violates the "same Kernel for production + simulation" invariant from
  Step 6.

Using the existing kernel mechanism keeps the orchestrator trivially small
and keeps the kernel as the single source of truth for execution semantics.

### 3. Concurrency boundary — the first one in PersonaOS

Two orchestrators running `process_one()` concurrently on the same task:

```
Orchestrator A: SELECT (finds task X, status='queued')
Orchestrator B: SELECT (finds task X, status='queued')     ← same task
        ↓
Orchestrator A: kernel.execute(X) → Phase 3 succeeds, X='in_progress'
Orchestrator B: kernel.execute(X) → Phase 3 fails, raises TaskStatusError
        ↓
Orchestrator A: tasks_processed += 1, returns True
Orchestrator B: claim_conflicts += 1, returns False
```

Result: only ONE orchestrator successfully processes the task. The other
catches `TaskStatusError`, moves on. **No locks, no leases, no distributed
consensus.** The atomic UPDATE is sufficient for a single-process v0.1.

### 4. Polling loop — tight when busy, sleeping when idle

```python
async def run(self) -> None:
    while not self._stop_requested:
        self._metrics.poll_iterations += 1
        processed = await self.process_one()
        if not processed and not self._stop_requested:
            await asyncio.sleep(self._poll_interval)
```

When work exists, the orchestrator processes immediately (no sleep). When
the queue is empty, it sleeps `poll_interval` seconds between polls. The
`poll_iterations` counter increments on every loop pass.

### 5. Metrics — simple in-memory counters

```python
@dataclass
class OrchestratorMetrics:
    tasks_processed: int = 0      # kernel.execute returned successfully
    claim_conflicts: int = 0      # TaskStatusError caught (someone else won)
    poll_iterations: int = 0      # loop passes
```

No Prometheus / OpenTelemetry integration in v0.1. Counters are in-memory;
process restart loses them. Deferred: persistent metrics, structured logs.

### 6. Adapter-agnostic — orchestrator doesn't know what the adapter does

The orchestrator does not import `OpenClawAdapter` or reference the OpenClaw
runtime. It only depends on the `WorkerExecutionKernel` and the Adapter
Protocol. Static check enforced by `test_orchestrator_does_not_import_openclaw`.

## Architecture

```
                         PersonaOS Control Plane
                                    │
                                    ▼
                             Workload / Queue
                                    │
            ┌───────────────────────┼───────────────────────┐
            ▼                       ▼                       ▼
     CTO Worker              Dev Worker              QA Worker
     orchestrator            orchestrator            orchestrator
            │                       │                       │
            └───────────────────────┼───────────────────────┘
                                    ▼
                          WorkerExecutionKernel
                                    │
                          ┌─────────┴─────────┐
                          ▼                   ▼
                   OpenClawAdapter    SimulationAdapter
                   (production)       (replay only)
```

For v0.1, all three orchestrators share the same kernel (and therefore the
same Adapter). They compete for tasks via the atomic claim. The
"CTO/Dev/QA" distinction is a future routing concern (per-worker profiles,
priority, dependencies) — all deferred.

## Invariants (locked)

| # | Invariant | Enforcement |
|---|---|---|
| 1 | Orchestrator uses the existing kernel — no parallel execution path | orchestrator calls `kernel.execute(task)` only |
| 2 | Atomic claim via the existing kernel mechanism (Phase 3) | no new claim API; relies on kernel's UPDATE WHERE status='queued' |
| 3 | Two orchestrators → only ONE claims per task | concurrency test (`test_two_orchestrators_only_one_claims_same_task`) |
| 4 | Adapter-agnostic | static check forbids `OpenClawAdapter`/`openclaw`/`OPENCLAW_API_URL` |
| 5 | Clean shutdown | `stop()` sets a flag, `run()` checks after each iteration |
| 6 | No leases/failover | static check; deferred to v0.2+ per ADR 0008 future evolution |
| 7 | Workers table is never touched | orchestrator only reads/writes `tasks` |
| 8 | Polling is bounded | `poll_interval` prevents a tight CPU loop when queue is empty |

## What's NOT in Step 7 (deferred)

| Deferred | Reason |
|---|---|
| Distributed workers | v0.1 is single-process; multi-process needs leases |
| Leases/failover | Needs `execution_node` + `lease_expires_at` columns (v0.2 per ADR 0008 future evolution) |
| `execution_node` column | Same as above — needed for node binding + failover |
| Automatic retry policy | Step 5 supports basic retry semantics; smarter policies are v0.2+ |
| Priority scheduling | Tasks are processed FIFO (ORDER BY created_at); priority is a v0.2 concern |
| Dependency scheduling | Tasks with `dependencies` field are blocked; resolver unblocked is v0.2+ |
| Autoscaling | Single orchestrator per process; scaling is operational, not in code |
| LLM supervisor | The adapter does the LLM call; orchestrator doesn't know about it |
| Multi-agent delegation | Different scope; needs planning + delegation semantics |
| Persistent metrics / OTel | v0.2 observability concern |
| `FOR UPDATE SKIP LOCKED` | v0.2 optimization for higher concurrency; the atomic UPDATE is sufficient for single-process |

For Step 7, the strongest first milestone is:

> **The orchestrator proves the polling-loop pattern works: find queued task,
> invoke kernel, handle the concurrency boundary, repeat. The first
> concurrency boundary in PersonaOS is exercised end-to-end.**

Multi-node scaling, leases, and failover are explicitly NOT in scope.

## Consequences

**Positive:**

- PersonaOS can now "run" — find work and execute it without manual triggers.
- First proof that the kernel works in a polling loop (not just as one-shot).
- The concurrency boundary is exercised by the test suite from day one.
- Clean shutdown for graceful process exit (`SIGTERM → stop() → loop exits`).
- The orchestrator is ~80 lines of code. It does almost nothing — by design.

**Negative:**

- One new package (`orchestrator/`) with one new class.
- Polling has CPU/timer considerations — handled by `poll_interval` (default 100ms).
- In-memory metrics only — process restart loses them (deferred: persistent metrics).
- No built-in restart on failure — if `kernel.execute()` raises a non-`TaskStatusError`,
  the orchestrator propagates it (caller's responsibility to catch and restart).

## Migration path

| Sub-step | Deliverable | Status |
|---|---|---|
| 7a | This ADR + spec + acceptance tests (this commit; tests skip until 7b) | **this commit** |
| 7b | `WorkerOrchestrator` + `OrchestratorMetrics` + unskip tests | next |
| 7c | Commit Step 7 as a single feature | bundled with 7b |

Target test count after Step 7: 53 (Step 6) + 9 (Step 7) = **62 passed, 0 skipped, 0 failed**.

## References

- [ADR 0008 — Thin Worker, six tables](0008-thin-worker-six-tables.md)
- [ADR 0009 — WorkerExecutionContext](0009-worker-execution-context.md)
- [ADR 0010 — WorkerExecutionKernel](0010-worker-execution-kernel.md)
- [ADR 0011 — Task Simulation / Replay](0011-task-simulation-replay.md)
- Spec: [`docs/specs/worker-orchestrator.md`](../specs/worker-orchestrator.md)
- Tests: `tests/integration/test_worker_orchestrator.py`