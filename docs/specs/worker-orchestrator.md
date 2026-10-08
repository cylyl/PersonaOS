# Worker Orchestrator Specification

**Status:** Draft v0.1 (2026-10-08)
**Companion to:** [ADR 0012 — Worker Orchestrator](../adr/0012-worker-orchestrator.md)

## 1. Concept

The `WorkerOrchestrator` is the persistent polling loop that drives the
existing `WorkerExecutionKernel`. It is what turns PersonaOS from a library
into a service.

```text
┌────────────────────────────────────────────┐
│           WorkerOrchestrator.run()         │
│                                            │
│   while not stop_requested:                │
│       ┌──────────────────────────┐         │
│       │  process_one():           │         │
│       │   1. Find queued task     │ ← best-effort SELECT
│       │   2. kernel.execute(task) │ ← atomic claim inside kernel
│       │   3. Catch TaskStatusError│ ← concurrency boundary
│       └──────────────────────────┘         │
│       if no work:                          │
│           await asyncio.sleep(poll_intvl) │
└────────────────────────────────────────────┘
                    │
                    ▼
          WorkerExecutionKernel
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
   OpenClawAdapter    SimulationAdapter
   (production)       (replay only)
```

The orchestrator's **only** job is to find work and invoke the kernel. It
does NOT own:

- the claim semantics (the kernel's Phase 3 does)
- the context resolution (the registry does)
- the adapter protocol (the kernel owns it)
- retries, failover, leases (all deferred per ADR 0012)

## 2. API

### 2.1 `WorkerOrchestrator`

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
        """Poll until stop() is called. Never returns unless stopped.

        Tight loop when busy, sleeps `poll_interval` when queue is empty.
        Exceptions from kernel.execute() other than TaskStatusError
        propagate to the caller.
        """

    async def process_one(self) -> bool:
        """One iteration. Returns True if a task was processed, False if
        the queue was empty or a concurrency conflict was caught.

        The concurrency boundary: if two orchestrators call process_one()
        on the same queued task, one succeeds (the kernel's atomic claim
        transitions queued → in_progress) and the other catches
        TaskStatusError (the kernel's Phase 3 found rowcount=0).
        """

    def stop(self) -> None:
        """Request clean shutdown. The run() loop checks this flag at
        the top of each iteration and after each asyncio.sleep."""

    @property
    def is_running(self) -> bool:
        """True if the run() loop is currently active."""

    @property
    def metrics(self) -> "OrchestratorMetrics":
        """Read-only access to the in-memory metrics counters."""
```

### 2.2 `OrchestratorMetrics`

```python
@dataclass
class OrchestratorMetrics:
    """In-memory counters. Process restart loses them."""
    tasks_processed: int = 0   # kernel.execute returned successfully
    claim_conflicts: int = 0   # TaskStatusError caught (someone else won)
    poll_iterations: int = 0   # run() loop passes
```

### 2.3 Constructor parameters

| Name | Purpose | Default |
|---|---|---|
| `session` | AsyncSession used to SELECT queued tasks | required |
| `kernel` | WorkerExecutionKernel to invoke per task | required |
| `poll_interval` | Seconds to sleep when queue is empty | `0.1` |

`session` and `kernel` are the same objects that a synchronous caller would
use (e.g., a test invoking `kernel.execute(task)` directly). The orchestrator
does NOT create its own session or kernel — that's the caller's job.

## 3. The flow

```python
# Setup (caller's job)
reg = ProfileRegistry(session=session, profiles_dir=profiles_dir)
await reg.register_worker(...)
await reg.create_profile_version(...)
await reg.activate_profile_version(..., 2)

# Enqueue work
wl = WorkloadService(session=session)
await wl.enqueue(worker_id="w1", title="A", objective="A", input={})
await wl.enqueue(worker_id="w1", title="B", objective="B", input={})

# Orchestrate
adapter = CapturingAdapter(result_to_return=Result(status="completed", output={"x": 1}))
kernel = WorkerExecutionKernel(
    session=session,
    adapter=adapter,
    resolve_context=reg.resolve_execution_context,
)
orchestrator = WorkerOrchestrator(session=session, kernel=kernel)

# Run in background
run_task = asyncio.create_task(orchestrator.run())

# ... wait for both tasks to be processed ...
while orchestrator.metrics.tasks_processed < 2:
    await asyncio.sleep(0.05)

# Stop
orchestrator.stop()
await run_task
```

## 4. Acceptance criteria

| # | Criterion | Test |
|---|---|---|
| 1 | Orchestrator processes a queued task (find → claim → execute → completed) | `test_orchestrator_processes_queued_task` |
| 2 | Multiple tasks processed in sequence | `test_orchestrator_processes_multiple_tasks_in_sequence` |
| 3 | Empty queue → no crash, returns False | `test_orchestrator_polls_when_queue_empty` |
| 4 | `stop()` causes `run()` to exit cleanly | `test_orchestrator_stops_cleanly` |
| 5 | Two orchestrators on the same queued task → only ONE claims | `test_two_orchestrators_only_one_claims_same_task` |
| 6 | Orchestrator is adapter-agnostic (no OpenClaw imports) | `test_orchestrator_does_not_import_openclaw` |
| 7 | Orchestrator uses existing kernel (no parallel execution path) | `test_orchestrator_uses_existing_kernel` |
| 8 | No leases / failover / execution_node (deferred, static check) | `test_orchestrator_no_leases_no_failover` |
| 9 | `poll_interval` defaults to 0.1s (no tight CPU loop) | `test_orchestrator_default_poll_interval` |

## 5. What's NOT in Step 7 (deferred)

Per ADR 0012 — the orchestrator is intentionally minimal:

| Deferred | Reason |
|---|---|
| Distributed workers | v0.1 is single-process; multi-process needs leases |
| Leases / failover | Needs `execution_node` + `lease_expires_at` (v0.2) |
| `execution_node` column | Same as above |
| Automatic retry policy | Step 5 covers basic retry; smarter policies are v0.2 |
| Priority scheduling | FIFO (created_at) for now |
| Dependency scheduling | Tasks with `dependencies` are blocked; resolver is v0.2+ |
| Autoscaling | Operational concern |
| LLM supervisor | The adapter does the LLM call |
| Multi-agent delegation | Different scope |
| Persistent metrics / OTel | v0.2 observability |
| `FOR UPDATE SKIP LOCKED` | v0.2 optimization for higher concurrency |

For Step 7, the strongest first milestone is:

> **The orchestrator proves the polling-loop pattern works: find queued
> task, invoke kernel, handle the concurrency boundary, repeat.**

Multi-node scaling, leases, and failover are explicitly NOT in scope. The
single-process atomic UPDATE is sufficient for v0.1.

## 6. Implementation order

| Sub-step | Deliverable |
|---|---|
| 7a | This spec + ADR 0012 + acceptance tests (this commit; tests skip until 7b) |
| 7b | `WorkerOrchestrator` + `OrchestratorMetrics` + unskip tests |
| 7c | Tests pass; commit Step 7 as a single feature |

Target test count after Step 7: 53 (Step 6) + 9 (Step 7) = **62 passed, 0 skipped, 0 failed**.