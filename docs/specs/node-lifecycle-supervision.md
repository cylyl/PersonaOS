# Specification: Node Lifecycle Supervision, Heartbeat Tracking, and Graceful Eviction

**Module:** `personaos.orchestrator.supervisor`, `personaos.execution.runner`, `personaos.worker_main`
**Related ADR:** ADR 0024, ADR 0003, ADR 0008, ADR 0016, ADR 0023
**Source of Truth:** `schemas/worker.yaml`, `schemas/task.yaml`, `VIKUNJA.md`

---

## 1. Lifecycle Architecture & Failover Flow

```
   Compute Node 1 (Unhealthy / Terminated)           PostgreSQL Database                Compute Node 2 (Healthy Survivor)
 ┌──────────────────────────────────────────┐     ┌───────────────────────┐            ┌────────────────────────────────┐
 │ WorkerRunner                             │     │                       │            │ WorkerRunner                   │
 │ (Heartbeat stops updating last_seen_at)  │     │ workers:              │            │                                │
 └────────────────────┬─────────────────────┘     │  - last_seen_at (stale│            └───────────────▲────────────────┘
                      │                           │ worker_nodes:         │                            │
                      ▼                           │  - unbound_at IS NULL │                            │
                 [Crash / Term]                   │ tasks:                │                            │
                                                  │  - status=in_progress │                            │
                                                  │  - assigned_node=N1   │                            │
                                                  └───────────▲───────────┘                            │
                                                              │                                        │
                                                              │ Inspects bindings                      │
                                                              │ & last_seen_at                         │
                                                              ▼                                        │
                                                  ┌───────────────────────┐                            │
                                                  │ NodeSupervisor        │                            │
                                                  │ (supervise_cycle)     │                            │
                                                  └───────────┬───────────┘                            │
                                                              │                                        │
                                            1. Mark unbound_at = NOW()                                 │
                                               reason = 'heartbeat_timeout'                            │
                                            2. Fast-expire task lock:                                  │
                                               lock_expires_at = NOW() - 1s                            │
                                                              │                                        │
                                                              ▼                                        │
                                                  ┌───────────────────────┐                            │
                                                  │ Immediate Reclaim     ├────────────────────────────┘
                                                  │ (Surviving Worker N2) │
                                                  └───────────────────────┘
```

---

## 2. Component Specifications

### 2.1 `NodeSupervisor` (`src/personaos/orchestrator/supervisor.py`)

#### Class Signature
```python
@dataclass
class SupervisionCycleResult:
    dead_nodes_evicted: int
    tasks_expired: int
    evicted_node_ids: list[str]
    expired_task_ids: list[str]


class NodeSupervisor:
    """Supervises compute node liveness, detects dead nodes, and accelerates recovery."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        default_heartbeat_timeout_seconds: float = 60.0,
    ) -> None: ...

    async def detect_dead_nodes(
        self,
        heartbeat_timeout_seconds: float | None = None,
    ) -> list[tuple[str, str]]:
        """Identify (worker_id, node_id) tuples whose bindings are active but heartbeats are stale."""

    async def evict_dead_nodes(
        self,
        heartbeat_timeout_seconds: float | None = None,
    ) -> SupervisionCycleResult:
        """Evict dead node bindings and accelerate task lease expiration."""

    async def run_loop(
        self,
        interval_seconds: float = 10.0,
        heartbeat_timeout_seconds: float = 60.0,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """Run periodic supervision in the background."""
```

#### SQL Query for Dead Node Detection
```sql
SELECT wn.id, wn.worker_id, wn.node_id
FROM worker_nodes wn
JOIN workers w ON w.id = wn.worker_id
WHERE wn.unbound_at IS NULL
  AND (
    (w.last_seen_at IS NOT NULL AND w.last_seen_at < NOW() - make_interval(secs => :timeout_seconds))
    OR
    (w.last_seen_at IS NULL AND wn.bound_at < NOW() - make_interval(secs => :timeout_seconds))
  )
```

#### Task Lease Accelerated Expiration
When a node `N` is evicted, any in-flight task assigned to `N` has its lease immediately expired so that surviving workers don't wait out standard lease timeouts:
```sql
UPDATE tasks
SET lock_expires_at = NOW() - interval '1 second'
WHERE status = 'in_progress'
  AND assigned_node = :node_id
  AND (lock_expires_at IS NULL OR lock_expires_at > NOW())
RETURNING id;
```

---

### 2.2 `WorkerRunner` Graceful Drain & Unbinding (`src/personaos/execution/runner.py`)

1. **Configurable Drain Timeout:**
   `drain_timeout: float = 30.0` passed to `__init__`.
2. **Graceful Drain Sequence:**
   - `stop()` requests orchestrator stop (`self._orchestrator.stop()`).
   - If orchestrator has active concurrent tasks, runner waits for them up to `drain_timeout`.
   - On shutdown, `WorkerNode` record updated with `unbound_at = NOW()` and `reason = "graceful_drain"` (or `"runner_shutdown"` if unassigned).
3. **Heartbeat Loop Cleanup:**
   - Cancel heartbeat task cleanly and await cancellation.

---

### 2.3 Production CLI Arguments (`src/personaos/worker_main.py`)

- `--concurrency` / `--max-concurrency`: Optional integer to limit or expand concurrent tasks.
- `--heartbeat-interval`: Float seconds between worker heartbeats (default: 30.0).
- `--drain-timeout`: Float seconds to wait for active tasks during graceful stop (default: 30.0).
- Wire `session_factory` and `max_concurrency` directly to `WorkerOrchestrator` and `WorkerRunner`.
