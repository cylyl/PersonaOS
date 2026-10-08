# Task Simulation / Replay Specification

**Status:** Draft v0.1 (2026-10-08)
**Companion to:** [ADR 0011 — Task Simulation / Replay](../adr/0011-task-simulation-replay.md)

## 1. Concept

Simulation/replay re-runs a historical task without mutating the original. It creates a NEW task linked to the source via `simulated_from` FK, and executes it through the SAME `WorkerExecutionKernel` as production — only the Adapter differs.

```
Historical Task                        Simulation
───────────────                        ──────────
  id = task-123                          id = task-NEW
  status = completed                     status = completed (after replay)
  profile_version = 2                    profile_version = 2   ← snapshot from source
  input = {...}                          input = {...}        ← copied from source
  result = <OpenClaw output>             result = <SimulationAdapter output>
  simulated_from = NULL                  simulated_from = task-123   ← the FK
       │                                       │
       └─────────────────────────────────────────┘
                          │
                          ▼
              WorkerExecutionContext
                          │
                          ▼
                WorkerExecutionKernel
                          │
              ┌───────────┴───────────┐
              ▼                       ▼
   OpenClawAdapter          SimulationAdapter
   (production)             (replay only)
```

## 2. Why replay?

- **Audit** — re-run a task that failed 6 months ago to verify the issue reproduces under the current kernel.
- **A/B testing** — compare v2 vs v3 persona outcomes on the same input.
- **Regression detection** — catch when a profile update silently changes task behavior.
- **Compliance** — prove historical executions were correct against the documented configuration.

## 3. Schema (migration 0003)

```sql
ALTER TABLE tasks
    ADD COLUMN simulated_from text
        REFERENCES tasks(id) ON DELETE SET NULL;
CREATE INDEX ix_tasks_simulated_from ON tasks (simulated_from);
```

The simulation is a regular row in `tasks` with the new column set. The original row is untouched.

`ON DELETE SET NULL` preserves simulations when the source is deleted — important for audit (we don't lose replay history just because someone cleaned up the source).

## 4. API

### 4.1 `SimulationService.replay`

```python
@dataclass(frozen=True)
class SimulationResult:
    """Outcome of a replay."""
    task_id: str                # the NEW simulation task
    result: Result              # the adapter's output
    source_task_id: str         # the source task (immutable)
    source_profile_version: int # the snapshot version used


class SimulationService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        kernel: WorkerExecutionKernel,
        registry: ProfileRegistry,
    ) -> None: ...
    
    async def replay(self, task_id: str) -> SimulationResult:
        """Replay the historical task.
        
        Creates a NEW task with simulated_from=task_id.
        The source task is NEVER mutated.
        Returns SimulationResult with the new task id, the adapter's Result,
        and the source linkage.
        
        Raises:
            SourceTaskNotFoundError: if task_id doesn't exist.
        """
```

### 4.2 `SimulationAdapter`

```python
class SimulationAdapter:
    """No-op adapter for replay testing. Deterministic Result."""
    
    async def execute(
        self,
        context: WorkerExecutionContext,
        checkpoint: Any | None = None,
    ) -> Result:
        return Result(
            status="completed",
            output={
                "simulated": True,
                "task_id": context.task.id,
                "profile_version": context.profile.version,
            },
        )
```

The adapter is intentionally minimal for Step 6. Future Adapters (e.g., a recording adapter that re-uses a cached OpenClaw response, or a model-pinned deterministic adapter) are deferred.

## 5. The flow

```python
# Setup
reg = ProfileRegistry(session, profiles_dir)
await reg.register_worker("w1", v1)
await reg.create_profile_version("w1", v2)
await reg.activate_profile_version("w1", 2)

# Production execution
wl = WorkloadService(session)
prod_adapter = CapturingOpenClawAdapter(returns=Result(status="completed", output={"produced": True}))
prod_kernel = WorkerExecutionKernel(session=session, adapter=prod_adapter, resolve_context=reg.resolve_execution_context)
task = await wl.enqueue(worker_id="w1", title="deploy", objective="...", input={"service": "api"})
await prod_kernel.execute(task)
# task.status == "completed", task.result == {"produced": True}

# Worker state changes (e.g., activate v3)
await reg.create_profile_version("w1", v3)
await reg.activate_profile_version("w1", 3)
# worker.current_profile_version == 3

# Replay
sim_adapter = SimulationAdapter()
sim_kernel = WorkerExecutionKernel(session=session, adapter=sim_adapter, resolve_context=reg.resolve_execution_context)
sim_service = SimulationService(session=session, kernel=sim_kernel, registry=reg)
result = await sim_service.replay(task.id)
# result.task_id is NEW (different from task.id)
# result.source_task_id == task.id
# result.source_profile_version == 2 (NOT 3)

# Verify source is unchanged
# task.status == "completed" (unchanged)
# task.result == {"produced": True} (unchanged)
# task.profile_version == 2 (unchanged)

# Verify simulation exists with FK
# (query) tasks WHERE simulated_from = task.id → finds result.task_id
# (query) tasks WHERE id = result.task_id → has simulated_from = task.id
```

## 6. Acceptance criteria

| # | Criterion | Test |
|---|---|---|
| 1 | Original task is immutable | `test_replay_preserves_source_task` |
| 2 | Historical profile is used | `test_replay_uses_historical_profile` |
| 3 | Historical input is used | `test_replay_uses_original_input` |
| 4 | Simulation isolated from OpenClaw | `test_simulation_does_not_import_openclaw` |
| 5 | Same Adapter contract used | `test_simulation_uses_adapter_protocol` |
| 6 | New result is on the simulation task | `test_simulation_result_on_new_task` |
| 7 | Auditable via `simulated_from` FK | `test_simulation_auditable_via_fk` |
| 8 | Worker state not mutated | `test_replay_does_not_mutate_worker_state` |
| 9 | Chain test (DB → domain → kernel → adapter → result) | `test_chain_test_full_path` |
| 10 | Existing 44 tests remain green | (verified by full suite run, not a single test) |

## 7. What's NOT in Step 6 (deferred)

Per ADR 0011 — deterministic same-result replay requires model-version pinning, tool-response capture, external-state freezing, etc. For Step 6, the strongest claim we make is:

> Same historical inputs and profile produce the same execution **configuration** (worker id, profile version, persona, instructions, runtime config), and `SimulationAdapter` proves the complete ORM → domain → profile → kernel → result chain.

Bit-exact result equality across runs is a later capability. This ADR explicitly does NOT promise it.

## 8. Implementation order

| Sub-step | Deliverable |
|---|---|
| 6a | This spec + ADR 0011 + acceptance tests (this commit; tests skip until 6b) |
| 6b | Migration 0003 + `SimulationService` + `SimulationAdapter` |
| 6c | Tests unskip, full suite passes (target 44 + 9 = **53 passed, 0 skipped, 0 failed**) |
| 6d | Commit as one atomic Step 6 feature |
