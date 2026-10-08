# ADR 0011 — Task Simulation / Replay

**Status:** Proposed (2026-10-08)
**Scope:** PersonaOS v0.1 extension (closes the loop on Steps 3-5)
**Supplements:** [ADR 0008 — Thin Worker, six tables](0008-thin-worker-six-tables.md), [ADR 0009 — WorkerExecutionContext](../adr/0009-worker-execution-context.md), [ADR 0010 — WorkerExecutionKernel](0010-worker-execution-kernel.md)

## Context

Steps 3-5 built a stable foundation:

- **Step 3** — WorkerProfile versioning + immutable snapshots on disk
- **Step 4** — WorkerExecutionContext (frozen bundle of worker + snapshot profile + task)
- **Step 5** — WorkerExecutionKernel (orchestrates lifecycle; Adapter-agnostic)

But there is no way to **replay** a historical task — to verify "if we re-ran task X today with the same profile + same input, what would happen?" That is critical for:

- **Audit** — re-run a task that failed 6 months ago to see if behavior changed
- **A/B testing** — compare v2 vs v3 persona outcomes on the same input
- **Regression detection** — catch when a profile update silently changes task behavior
- **Compliance** — prove historical executions were correct against the documented configuration

Step 6 closes the loop by adding simulation/replay.

## Decision

### 1. Schema — add `tasks.simulated_from` FK column

```sql
ALTER TABLE tasks
    ADD COLUMN simulated_from text
        REFERENCES tasks(id) ON DELETE SET NULL;
CREATE INDEX ix_tasks_simulated_from ON tasks (simulated_from);
```

The simulation is a NEW row in `tasks` with a FK to the source. The original task is unchanged. The simulation row carries:

| Field | Value |
|---|---|
| `simulated_from` | source task id (the new FK) |
| `type` | `'simulation'` (existing column repurposed for marker) |
| `profile_version` | source.profile_version (snapshot, frozen) |
| `input` | copy of source.input |
| `assigned_worker` | source.assigned_worker |
| `result` | adapter output during replay |

Cost: one nullable FK column + index. No new table.

**Why FK + nullable, not a separate `simulations` table:**

- Single-table query for "show me every simulation that has ever replayed task X" — indexed SELECT, not a JOIN + JSONB scan.
- The immutability invariant is explicit at the schema level: original task has `simulated_from IS NULL`; simulation has it set.
- ON DELETE SET NULL: deleting a source task preserves its simulations (FK becomes NULL but row stays) — important for audit.
- Stays inside the six-table boundary from ADR 0008.

### 2. SimulationService — `src/personaos/simulation/service.py`

```python
@dataclass(frozen=True)
class SimulationResult:
    """Outcome of a replay."""
    task_id: str                # the NEW simulation task
    result: Result              # the adapter's output
    source_task_id: str         # the source task (immutable)
    source_profile_version: int # the snapshot version used


class SimulationService:
    """Replays a historical task without mutating it."""
    
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
        """
```

### 3. Architecture — same Kernel, different Adapter

```
Production execution                          Simulation / replay
─────────────────────                         ────────────────────
WorkloadService.enqueue(task)                  SimulationService.replay(source_task_id)
        ↓                                              ↓
resolve_context(task)                          load source task from DB
        ↓                                              ↓
kernel.execute(task)                           create new task
        ↓                                       (simulated_from=source.id)
OpenClawAdapter                                        ↓
        ↓                                       resolve_context(sim_task)
Result → DB on task                           (reads source.profile_version)
                                                       ↓
                                              kernel.execute(sim_task)
                                                       ↓
                                              SimulationAdapter
                                                       ↓
                                              Result → DB on sim_task
```

The SAME `WorkerExecutionKernel` handles both. Only the Adapter differs.

The kernel does NOT know it's a simulation. From its perspective, it executes a task with whatever Adapter it's configured with and whatever `resolve_context` returns. The simulation is achieved by:

1. Different Adapter (`SimulationAdapter` vs `OpenClawAdapter`)
2. Different task (production vs new simulation with `simulated_from` FK)
3. Different context (kernel reads `sim_task.profile_version` = source's snapshot, never worker.current)

### 4. SimulationAdapter — `src/personaos/simulation/adapter.py`

A real `Adapter` implementation, not a stub:

```python
class SimulationAdapter:
    """No-op adapter for replay testing.
    
    Returns a deterministic Result based on the context, proving the
    chain works without requiring a real agent engine. Does NOT call
    OpenClaw or any external service.
    """
    
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

Static guarantee: `simulation/adapter.py` MUST NOT import or reference `OpenClawAdapter`, `openclaw`, or `OPENCLAW_API_URL`. (Test enforces.)

## Invariants (locked)

| # | Invariant | Enforcement |
|---|---|---|
| 1 | Original task is immutable | `SimulationService.replay` only INSERTs; never UPDATEs the source |
| 2 | Historical profile is used | sim_task.profile_version = source.profile_version (snapshotted at replay) |
| 3 | Historical input is used | sim_task.input = copy of source.input (no transformation) |
| 4 | Simulation isolated from OpenClaw | `SimulationAdapter` is a separate class; static check forbids OpenClaw references |
| 5 | Same Adapter contract | `SimulationAdapter` conforms to `Adapter` Protocol |
| 6 | Simulation produces new result | sim_task.result = adapter output; source.result unchanged |
| 7 | Simulation is auditable | `tasks.simulated_from` FK links sim_task to source; queryable via index |
| 8 | No worker state mutation | replay never UPDATEs `workers` table |

## What's NOT in Step 6 (deferred)

| Deferred | Reason |
|---|---|
| Deterministic same-result replay | LLM non-determinism; needs model-version pinning, temperature lock, tool-response capture |
| Tool-response capture | External state varies across runs |
| External state freezing | Network/time/randomness varies |
| Multi-step replay with state across steps | Different shape; needs a simulator loop |
| A/B test harness | Builds on replay but adds experiment logic |
| Diff between original + replay result | UI-level concern |
| Cleanup of stale simulations | Operational concern; later |
| Cross-task replay (replaying a DAG) | Different scope; needs dependency-graph traversal |

**For Step 6, the strongest first milestone is:**

> Same historical inputs and profile produce the same execution **configuration** (worker id, profile version, persona, instructions, runtime), and `SimulationAdapter` proves the complete ORM → domain → profile → kernel → result chain.

True bit-exact determinism is a later capability — this ADR explicitly does NOT promise it.

## Consequences

**Positive:**

- The full chain (DB row → `Task.from_orm` → `resolve_execution_context` → `kernel.execute` → Adapter → `Result` → DB row) is exercised end-to-end.
- Original tasks remain untouched — replay never corrupts history.
- Replay is auditable via a single indexed FK.
- Same kernel + different adapter proves the abstraction is real, not just two execution engines that happen to look similar.

**Negative:**

- One new migration (0003 adds the `simulated_from` column).
- One new Adapter implementation (`SimulationAdapter` — currently a no-op).
- Replay creates a new task per call — could grow the `tasks` table. Mitigation (deferred): TTL/cleanup policy in Step 7+.

## Migration path

| Sub-step | Deliverable | Status |
|---|---|---|
| 6a | This ADR + spec + acceptance tests (this commit; tests skip until 6b) | **this commit** |
| 6b | Migration 0003 + `SimulationService` + `SimulationAdapter` + `_resolve_context` exposed on kernel if needed | next |
| 6c | Tests unskip and pass; commit Step 6 as a single feature | bundled with 6b |

Target test count after Step 6: 44 (Steps 3-5) + 9 (Step 6) = **53 passed, 0 skipped, 0 failed**.

## References

- [ADR 0008 — Thin Worker model, six tables for v0.1](0008-thin-worker-six-tables.md)
- [ADR 0008 Addendum — Versioned WorkerProfile + Immutable Task Snapshots](0008-addendum-worker-profile-versioning.md)
- [ADR 0009 — WorkerExecutionContext](0009-worker-execution-context.md)
- [ADR 0010 — WorkerExecutionKernel](0010-worker-execution-kernel.md)
- Spec: [`docs/specs/task-simulation-replay.md`](../specs/task-simulation-replay.md)
- Tests: `tests/integration/test_task_simulation.py`
