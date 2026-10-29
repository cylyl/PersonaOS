# ADR 0033: Multi-Node Control-Plane Distribution

**Status:** Accepted
**Date:** 2026-10-29
**Deciders:** PersonaOS Architecture Team
**Consulted:** Control Plane, Workload Execution, Platform Engineering, Database Engineering
**Informed:** Node Operators, Runtime Adapters, Security Team

---

## Context & Problem Statement

In PersonaOS v0.1, the control plane (FastAPI API service, workload scheduler, review resolution lifecycle, and node supervisor) operates primarily as a single-process or co-located multi-process deployment on a single machine. While all state mutations are durably committed to PostgreSQL (using `FOR UPDATE SKIP LOCKED` queues, append-only event logs, and generation fencing per ADR 0010, 0015, and 0016), scaling to enterprise workloads and achieving high availability requires distributing the control plane across multiple physical or virtual hosts.

Key challenges in multi-node control-plane distribution:
1. **Concurrent Scheduling & Claims:** Multiple control-plane nodes must safely claim tasks, evaluate DAG readiness, and assign work without double-claiming or deadlocking.
2. **Split-Brain & Partitioning:** If network partitions isolate control-plane instances, stale or partitioned nodes must not corrupt state or issue conflicting instructions.
3. **Operational Simplicity & Operational Overhead:** Introducing complex external distributed consensus clusters (e.g., etcd, Apache ZooKeeper, or Consul) increases infrastructure surface area, deployment friction, and operational failure modes.
4. **Resilient Failover:** Control-plane node crashes or restarts must not cause task starvation, hung workflows, or inconsistent worker supervision.

---

## Decision Drivers

- **PostgreSQL as Sole Source of Truth:** Preserve the core architecture principle (ADR 0001, ADR 0015) that PostgreSQL provides ACID guarantees and durable state storage.
- **Active-Active Scalability:** Allow any number of control-plane instances (`control-plane-01`, `control-plane-02`, etc.) to run concurrently behind standard HTTP/TCP load balancers without designated active-passive bottlenecks.
- **Deterministic Fencing:** Guarantee zero duplicate side effects and complete split-brain immunity using generation tokens and database-level transactional locks.
- **Zero External Coordination Daemons:** Avoid requiring Raft-based distributed key-value stores (etcd/Consul) by utilizing PostgreSQL row-level locks, advisory locks, and conditional updates.

---

## Considered Options

1. **Option 1: Raft-based Distributed State Store (etcd/Consul).**
   - *Pros:* Native leader election, distributed KV watch streams.
   - *Cons:* Introduces dual sources of truth (etcd + PostgreSQL), requires two-phase commit or distributed sagas to maintain consistency, dramatically raises deployment complexity.
2. **Option 2: Active-Passive Control Plane with VIP / Pacemaker.**
   - *Pros:* Simple single-writer conceptual model.
   - *Cons:* Idle standby capacity, failover delay (30–60s), split-brain risk during fencing network partitions (STONITH complexity).
3. **Option 3: Active-Active Leaderless Control Planes with PostgreSQL Concurrency & Fencing (Selected).**
   - *Pros:* Fully stateless control-plane instances, elastic horizontal scaling, zero new external infrastructure, deterministic concurrency via `FOR UPDATE SKIP LOCKED`, monotonic generation fencing prevents split-brain writes.
   - *Cons:* PostgreSQL connection pooling and transaction rate become the primary scaling dimensions (mitigated by PgBouncer and optimized index-backed claim queries).

---

## Decision

We will implement **Multi-Node Active-Active Control-Plane Distribution** backed natively by PostgreSQL:

### 1. Control-Plane Node Architecture & Identity
- Every control-plane instance generates a unique immutable node identity upon startup: `control_plane_node_id = f"cp-{hostname}-{uuid4().hex[:8]}"`.
- Instances register in a durable `control_plane_nodes` table with heartbeat timestamps (`last_heartbeat_at`, `status = 'active'`, `version`, `api_endpoint`).
- Any control-plane node can handle incoming REST requests, submit tasks, approve reviews, or trigger recovery scans.

### 2. Autonomous Distributed Task Scheduling & Claims
- Schedulers run concurrently across all control-plane instances.
- Task claim concurrency is governed by PostgreSQL `SELECT ... FOR UPDATE SKIP LOCKED` on indexed runnable tasks:
  ```sql
  SELECT id FROM tasks
  WHERE status = 'pending'
    AND runnable = true
  ORDER BY priority DESC, created_at ASC
  LIMIT :batch_size
  FOR UPDATE SKIP LOCKED;
  ```
- Workers and nodes are bound transactionally. When two control-plane nodes attempt to assign or route the same task, `SKIP LOCKED` ensures exactly one acquires the lock, while the other advances immediately to subsequent runnable candidates.

### 3. Generation Fencing & Split-Brain Immunity
- All mutable state transitions (claims, checkpoints, review resolutions, timeouts, completions) require atomic generation checks:
  ```sql
  UPDATE tasks
  SET status = :new_status,
      generation = generation + 1,
      lock_expires_at = NOW() + :lease_ttl,
      updated_at = NOW()
  WHERE id = :task_id
    AND generation = :expected_generation;
  ```
- If a partitioned or lagging control-plane node attempts an update using a stale generation token, the SQL update affects `0` rows and fails with `StaleGenerationError`.
- No distributed consensus cluster (etcd) is required because PostgreSQL enforces serializability and linearizable commit ordering on task rows.

### 4. Distributed Background Supervisors
- Periodic reconciliation tasks (e.g., dead worker node eviction, orphan task recovery, metric aggregation) use PostgreSQL advisory locks:
  ```python
  # PostgreSQL transactional advisory lock for singleton periodic sweeps
  await session.execute(text("SELECT pg_try_advisory_xact_lock(:lock_id)"))
  ```
- If Node A holds the advisory lock during its recovery sweep, Node B's sweep attempt immediately skips without waiting or blocking, preventing redundant database scans.

---

## Component Topology

```mermaid
flowchart TD
    subgraph Client / Gateway Tier
        LB[Load Balancer / Reverse Proxy]
        CLI[PersonaOS CLI / Web Console]
        CLI --> LB
    end

    subgraph Control Plane Cluster (Active-Active)
        CP1["Control Plane Node 1 (cp-hostA-9f2d)<br>FastAPI + Scheduler + Supervisor"]
        CP2["Control Plane Node 2 (cp-hostB-4a1e)<br>FastAPI + Scheduler + Supervisor"]
        CP3["Control Plane Node 3 (cp-hostC-7b8c)<br>FastAPI + Scheduler + Supervisor"]
        LB --> CP1
        LB --> CP2
        LB --> CP3
    end

    subgraph Durable Coordination & Storage
        PG[(PostgreSQL Primary<br>State + Claims + Events)]
        CP1 <-->|FOR UPDATE SKIP LOCKED<br>Generation Fencing| PG
        CP2 <-->|FOR UPDATE SKIP LOCKED<br>Generation Fencing| PG
        CP3 <-->|FOR UPDATE SKIP LOCKED<br>Generation Fencing| PG
    end

    subgraph Remote Worker Fleet
        W1[Remote Worker Daemon 1]
        W2[Remote Worker Daemon 2]
        W3[Remote Worker Daemon 3]
        CP1 -.->|mTLS / RPC| W1
        CP2 -.->|mTLS / RPC| W2
        CP3 -.->|mTLS / RPC| W3
    end
```

---

## Consequences

### Positive
- **Linear Horizontal Scalability:** Control-plane nodes can be dynamically added or removed without rebalancing or partition realignment.
- **Split-Brain Protection:** Monotonic generation tokens ensure that even in a network partition, stale control-plane nodes cannot commit invalid mutations.
- **High Availability:** If a control-plane node crashes, other nodes automatically service REST requests and claim remaining tasks without failover delay.
- **Architectural Cohesion:** Retains PostgreSQL as the single source of truth, avoiding the operational complexity of etcd or Consul.

### Negative / Trade-offs
- **Database Connection Load:** Multiple control plane instances create more concurrent PostgreSQL connections. This requires connection pooling (e.g., PgBouncer or SQLAlchemy async pool sizing).
- **Advisory Lock Scope:** Distributed singletons (like reaper sweeps) must be carefully designed so that missed sweeps due to lock contention are retried within bounded intervals.
