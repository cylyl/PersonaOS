# ADR 0011 — SimulationAdapter and Task Replay

**Status:** Proposed (2026-10-21)  
**Scope:** PersonaOS v0.1 (simulation & deterministic replay)  
**Supplements:** [ADR 0009 — WorkerExecutionContext](0009-worker-execution-context.md), [ADR 0010 — WorkerExecutionKernel](0010-worker-execution-kernel.md)

## Context

PersonaOS establishes that "compute is replaceable; the worker is persistent." In Step 4 (ADR 0009), `WorkerExecutionContext` was designed as a frozen snapshot bundling worker identity, task inputs, and historical profile version. In Step 5 (ADR 0010), `WorkerExecutionKernel` orchestrates task execution through the `Adapter` protocol.

Both ADR 0009 (§Consequences) and ADR 0010 (§3 & §9) explicitly reserved Step 6 for **Simulation and Replay**:
> "Step 6 (simulation/replay) has a clean unit of replay: a historical task + its context."
> "SimulationAdapter (Step 6 — uses the kernel + a SimulationAdapter)"

Without a simulation adapter, tests and workload projections must either spin up live external runtimes (like OpenClaw or LLMs) or handcraft ad-hoc mocks. Furthermore, teams cannot safely evaluate counterfactual questions (e.g. "would worker profile v2 have successfully solved the task that failed under v1?") without executing live side effects or mutating historical audit records.

## Decision

### 1. SimulationAdapter Protocol Conformance

`SimulationAdapter` implements the `Adapter` Protocol defined in `execution/adapters/base.py`:

```python
class SimulationAdapter:
    async def execute(
        self,
        context: WorkerExecutionContext,
        checkpoint: Any | None = None,
    ) -> Result: ...
```

It provides:
- **Canned/Static mode**: Return predetermined `Result` objects.
- **Scripted mode**: Execute a custom synchronous or asynchronous callable `(context, checkpoint) -> Result`.
- **Sequential multi-step mode**: Yield successive results or checkpoints across multiple execution iterations.
- **Persona-aware default mode**: Simulate execution outputs deterministically based on `context.profile` attributes (autonomy, tools, skills).
- **Inspection & Tracing**: Records all incoming `WorkerExecutionContext` instances and checkpoint states for assertion and audit.

### 2. Unit of Replay: Historical Task + Snapshot Profile

Replay leverages the invariant established in ADR 0009: `tasks.profile_version` is an immutable record of the exact profile active at enqueue time.

`ReplayService.replay()`:
1. Loads the historical task from `tasks`.
2. Resolves its execution context via `ProfileRegistry` using `task.profile_version` (or an optional `profile_version_override`).
3. Loads the initial checkpoint (if `task.checkpoint_ref` exists).
4. Executes the adapter in memory.
5. Produces a `ReplayResult` comparing the new execution output against the historical `task.result` / `task.last_error`.
6. **Preserves immutability**: The historical task row and its events are untouched.

### 3. Counterfactual Simulation

To evaluate how profile changes impact performance, `ReplayService` supports `profile_version_override`:
- The replay engine resolves `profiles/{worker_id}/v{override}.yaml` instead of the original version.
- Compares the counterfactual outcome with the historical outcome.
- Allows regression detection and A/B evaluation before promoting profile versions in production.

### 4. Fork-and-Replay for Live Kernel Execution

To execute a replay through the full `WorkerExecutionKernel` and persist it with independent lifecycle tracking, `ReplayService.fork_and_replay()`:
- Enqueues a new task with `type="replay"`.
- Clones inputs, required skills, and capabilities.
- Retains or overrides the profile version snapshot.
- Emits standard `task.created` events and allows the kernel to execute it cleanly.

### 5. Completion of Enqueue Event Emission

Per ADR 0006 and ADR 0010 §6, `WorkloadService.enqueue` emits `task.created` atomically within the task insertion transaction.

## Invariants

| # | Invariant | Enforcement |
|---|---|---|
| 1 | SimulationAdapter conforms to `Adapter` Protocol | Type hint check `_: Adapter = SimulationAdapter` |
| 2 | Historical tasks are immutable during replay | `ReplayService.replay()` does not update the original `tasks` row |
| 3 | Replay resolves historical context by snapshot | Uses `task.profile_version` by default |
| 4 | Counterfactual override does not mutate worker's current version | `worker.current_profile_version` remains untouched |
| 5 | Enqueue emits `task.created` | Emitted in `WorkloadService.enqueue` within the same transaction |

## References

- [ADR 0008 — Thin Worker model, six tables for v0.1](0008-thin-worker-six-tables.md)
- [ADR 0009 — WorkerExecutionContext](0009-worker-execution-context.md)
- [ADR 0010 — Worker Execution Kernel](0010-worker-execution-kernel.md)
- Spec: [`docs/specs/simulation-and-replay.md`](../specs/simulation-and-replay.md)
