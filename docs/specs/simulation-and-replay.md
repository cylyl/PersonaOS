# Simulation and Replay Specification

**Status:** Draft v0.1 (2026-10-21)  
**Companion to:** [ADR 0011 — SimulationAdapter and Task Replay](../adr/0011-simulation-and-replay-adapter.md)

## 1. Concept

Step 6 delivers two core capabilities for PersonaOS:
1. **SimulationAdapter**: A swappable, deterministic runtime adapter adhering to the `Adapter` Protocol that can execute canned responses, scripts, multi-step checkpoint transitions, or mock persona behaviors.
2. **ReplayService**: An engine to replay historical tasks using their snapshotted execution contexts, evaluate counterfactual profile versions, and fork tasks for live kernel simulation.

## 2. SimulationAdapter Specification

`src/personaos/execution/adapters/simulation.py`

### Class Signature

```python
class SimulationAdapter:
    def __init__(
        self,
        *,
        result: Result | None = None,
        handler: Callable[[WorkerExecutionContext, Any | None], Result | Awaitable[Result]] | None = None,
        sequence: list[Result] | None = None,
        error_trigger: Exception | None = None,
    ) -> None: ...
```

### Execution Priority

When `execute(context, checkpoint)` is called:
1. If `error_trigger` is configured, raise it directly (verifying kernel exception handling).
2. If `handler` is configured, invoke it with `(context, checkpoint)` and return or await the result.
3. If `sequence` is configured, pop and return the next `Result` in the sequence.
4. If `result` is configured, return a copy of the canned `Result`.
5. Default behavior: Return `Result(status="completed", output={"simulated": True, "worker_id": context.worker.id, "profile_version": context.profile.version})`.

### State Inspection

`SimulationAdapter` tracks:
- `captured_contexts: list[WorkerExecutionContext]`
- `captured_checkpoints: list[Any | None]`
- `call_count: int`
- `reset()` method to clear captured state between test runs.

## 3. ReplayService Specification

`src/personaos/simulation/service.py`

### Replay Result Dataclass

```python
@dataclass(frozen=True)
class ReplayResult:
    task_id: str
    original_profile_version: int
    executed_profile_version: int
    original_status: str
    replayed_status: str
    original_output: dict | None
    replayed_output: dict | None
    original_error: str | None
    replayed_error: str | None
    matched: bool
    diff: dict
```

### Core Methods

```python
class ReplayService:
    def __init__(
        self,
        session: AsyncSession,
        registry: ProfileRegistry,
        default_adapter: Adapter | None = None,
    ) -> None: ...

    async def replay(
        self,
        task_id: str,
        adapter: Adapter | None = None,
        profile_version_override: int | None = None,
    ) -> ReplayResult: ...

    async def fork_and_replay(
        self,
        task_id: str,
        profile_version_override: int | None = None,
        priority: str | None = None,
    ) -> Task: ...
```

- `replay()`: Reads historical task, loads initial checkpoint (if any), resolves context for the task snapshot (or override), invokes adapter, and evaluates matching without altering DB rows.
- `fork_and_replay()`: Clones task inputs and objective into a new queued task with `type="replay"` and the specified profile version.

## 4. Acceptance Criteria

| # | Criterion | Verification |
|---|---|---|
| 1 | SimulationAdapter conforms to `Adapter` Protocol | Conformance marker `_: Adapter = SimulationAdapter` |
| 2 | Kernel executes successfully with SimulationAdapter | `test_simulation_adapter_with_kernel_success` |
| 3 | Kernel handles SimulationAdapter exceptions | `test_simulation_adapter_with_kernel_exception` |
| 4 | Checkpoint sequence passes through SimulationAdapter | `test_simulation_adapter_sequence_with_checkpoints` |
| 5 | Replay loads exact historical snapshot profile | `test_replay_uses_historical_task_snapshot` |
| 6 | Counterfactual replay uses overridden profile without mutating DB | `test_counterfactual_replay_profile_override` |
| 7 | Historical task immutability during replay | `test_historical_task_is_not_mutated_by_replay` |
| 8 | Fork-and-replay creates independent runnable task | `test_fork_and_replay_enqueues_new_task` |
| 9 | Enqueue emits `task.created` event | `test_enqueue_emits_task_created_event` |
