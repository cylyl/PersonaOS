# ADR 0024: Node Lifecycle Supervision, Heartbeat Tracking, and Graceful Eviction

**Status:** Accepted (Step 21 Implementation Phase)
**Date:** 2026-10-23
**Deciders:** PersonaOS Architecture Team
**Consulted:** Execution Runner, Worker Orchestrator, Workload Service, Kernel
**Informed:** Operators, Platform Engineers

---

## Context & Problem Statement

PersonaOS operates on the core thesis: **“Compute is replaceable; the worker is persistent.”**
Execution compute nodes (containers, VMs, spot instances) can be shut down, rescheduled, preempted, or crash at any time. When compute replacement occurs:
1. The persistent identity, memory, profiles, and state of the worker must survive without corruption or loss.
2. In-flight work should be given an opportunity to finish cleanly (graceful drain) or save a checkpoint.
3. If compute terminates abruptly (e.g. SIGKILL, node power loss, host failure), dead nodes must be detected promptly through automated heartbeat supervision, rather than leaving tasks stranded for 15+ minutes under static lease timeouts.

In Step 20, we implemented multi-task concurrency pooling and capacity gating up to `max_concurrent_tasks`. In Step 21, we address the node lifecycle and failure boundary:
- How compute node heartbeats are tracked against authoritative storage without adding schema migrations.
- How graceful shutdown signals (SIGINT, SIGTERM) cleanly drain in-flight tasks and record unbinding.
- How dead nodes are detected and evicted by a `NodeSupervisor`, immediately expiring stranded task locks so replacement compute can immediately resume and reclaim idempotent tasks.

---

## Decision

We will implement **Node Lifecycle Supervision, Heartbeat Tracking, and Graceful Eviction**:

### 1. Zero-Migration Heartbeat & Node Binding Model
- PersonaOS v0.1 schema already provides:
  - `workers.last_seen_at` (`TIMESTAMPTZ NULL`)
  - `worker_nodes` (`id`, `worker_id`, `node_id`, `bound_at`, `unbound_at`, `reason`)
  - `tasks.lock_expires_at` (`TIMESTAMPTZ NULL`), `tasks.status`, `tasks.assigned_node`
- `WorkerRunner` periodically updates `workers.last_seen_at = func.now()` at configurable `--heartbeat-interval` (default: 30 seconds).
- Active compute binding is represented by `worker_nodes.unbound_at IS NULL`.

### 2. Graceful Drain in `WorkerRunner`
- When SIGINT or SIGTERM is received (or `runner.stop()` is invoked):
  - Runner transitions to a draining state (`is_draining = True`).
  - Worker orchestrator stops taking new tasks from the queue (`stop()`).
  - Runner waits up to a configurable drain timeout (default: 30.0s) for active concurrent tasks to complete execution and flush checkpoints.
  - Upon completion or timeout, the runner updates its `worker_nodes` binding with `unbound_at = NOW()` and `reason = "graceful_drain"`.
  - Heartbeat background task is cancelled cleanly.

### 3. Automated Dead Node Detection and Eviction (`NodeSupervisor`)
- We introduce `NodeSupervisor` in `src/personaos/orchestrator/supervisor.py`:
  - Scans active bindings (`worker_nodes.unbound_at IS NULL`) joined with `workers`.
  - A node binding is considered dead if:
    ```sql
    worker_nodes.unbound_at IS NULL
    AND (
        (workers.last_seen_at IS NOT NULL AND workers.last_seen_at < NOW() - :heartbeat_timeout)
        OR (workers.last_seen_at IS NULL AND worker_nodes.bound_at < NOW() - :heartbeat_timeout)
    )
    ```
  - **Eviction Action:**
    1. Unbinds the dead binding: `UPDATE worker_nodes SET unbound_at = NOW(), reason = 'heartbeat_timeout'`.
    2. **Immediate Lock Expiration:**
       Instead of waiting for the full 15-minute task lease timeout, tasks in `status = 'in_progress'` assigned to the dead node/worker have their leases expired immediately:
       ```sql
       UPDATE tasks
       SET lock_expires_at = NOW() - interval '1 second'
       WHERE status = 'in_progress'
         AND assigned_node = :node_id
         AND (lock_expires_at IS NULL OR lock_expires_at > NOW())
       ```
    3. Replacement compute or surviving workers can immediately reclaim eligible tasks via `kernel.recover_in_progress()`, restoring worker throughput without human intervention.

### 4. CLI Argument Wiring in `worker_main.py`
- Expose production CLI arguments:
  - `--concurrency` / `--max-concurrency`: Override worker concurrency limit.
  - `--heartbeat-interval`: Interval for runner heartbeats.
  - `--drain-timeout`: Maximum wait time during graceful shutdown.

---

## Consequences

### Positive
- Zero schema migrations required; works within existing PostgreSQL tables.
- Compute failures are detected and remediated within the heartbeat timeout window (e.g. 60 seconds) rather than 15+ minutes.
- Safe, non-blocking graceful drains prevent half-executed tasks from being corrupted or aborted abruptly.
- Preserves the PersonaOS core invariant: compute dies, but worker identity and state persist.

### Negative / Trade-offs
- Setting heartbeat timeout too short (e.g. < 5 seconds) in high-latency network conditions could trigger false-positive evictions. Sensible default is 60 seconds.
- NodeSupervisor must run periodically (e.g. every 10-30 seconds) in a supervision loop or scheduled background process.
