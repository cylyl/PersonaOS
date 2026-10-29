# Specification: Multi-Node Control-Plane Distribution

**Reference:** ADR 0033, Vikunja Milestone v0.2-A
**Module:** `personaos.control_plane`, `personaos.workload`, `personaos.api`
**Source of Truth:** `schemas/control_plane.yaml`, `schemas/task.yaml`, `schemas/event.yaml`

---

## 1. System Architecture Overview & Component Diagram

The PersonaOS v0.2 control plane operates as an **active-active, leaderless cluster** where every instance provides full API handling, task scheduling, and lifecycle supervision. Concurrency and consistency are anchored entirely in PostgreSQL through row-level locking (`FOR UPDATE SKIP LOCKED`), monotonic generation fencing, and transactional advisory locks.

```mermaid
flowchart TD
    subgraph Client Access
        Client[CLI / Web Console / SDK]
        LB[Layer-4/7 Load Balancer<br>HAProxy / AWS ALB / NGINX]
        Client --> LB
    end

    subgraph Control Plane Cluster (Active-Active)
        subgraph CP1["Control Plane Node 1 (cp-alpha-01)"]
            API1[FastAPI REST API]
            Sched1[Workload Scheduler Engine]
            Sup1[Lifecycle Supervisor & Sweeper]
        end
        subgraph CP2["Control Plane Node 2 (cp-beta-02)"]
            API2[FastAPI REST API]
            Sched2[Workload Scheduler Engine]
            Sup2[Lifecycle Supervisor & Sweeper]
        end
        subgraph CP3["Control Plane Node 3 (cp-gamma-03)"]
            API3[FastAPI REST API]
            Sched3[Workload Scheduler Engine]
            Sup3[Lifecycle Supervisor & Sweeper]
        end
        LB --> API1
        LB --> API2
        LB --> API3
    end

    subgraph Durable Coordination & Data Tier
        PGBool[PgBouncer Connection Pooler]
        API1 --> PGBool
        Sched1 --> PGBool
        Sup1 --> PGBool
        API2 --> PGBool
        Sched2 --> PGBool
        Sup2 --> PGBool
        API3 --> PGBool
        Sched3 --> PGBool
        Sup3 --> PGBool

        PG[(PostgreSQL 16 Cluster<br>Primary + Synchronous Standby)]
        PGBool --> PG
    end

    subgraph Remote Worker Daemons
        RWD1[Remote Worker Daemon 1]
        RWD2[Remote Worker Daemon 2]
        RWD3[Remote Worker Daemon 3]
        CP1 <==>|mTLS WebSocket / JSON-RPC| RWD1
        CP2 <==>|mTLS WebSocket / JSON-RPC| RWD2
        CP3 <==>|mTLS WebSocket / JSON-RPC| RWD3
    end
```

---

## 2. Database Schema & Concurrency Model

### 2.1 Schema: `control_plane_nodes` Table (Migration 0007)

```sql
CREATE TABLE control_plane_nodes (
    id VARCHAR(64) PRIMARY KEY,              -- e.g. "cp-worker-prod-us-east-1a-9f2d"
    hostname VARCHAR(255) NOT NULL,
    ip_address VARCHAR(45) NOT NULL,
    port INTEGER NOT NULL,
    api_endpoint VARCHAR(512) NOT NULL,      -- e.g. "https://cp-1.internal:8000"
    status VARCHAR(32) NOT NULL DEFAULT 'active', -- 'active', 'draining', 'offline'
    version VARCHAR(32) NOT NULL,            -- e.g. "0.2.0"
    last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX idx_cp_nodes_status_heartbeat
ON control_plane_nodes (status, last_heartbeat_at DESC);
```

### 2.2 Concurrency Model: Autonomous Task Claims (`SKIP LOCKED`)

Multiple scheduler loops running on different control-plane instances query for runnable tasks concurrently. To eliminate lock contention and prevent double claims, schedulers execute:

```sql
WITH candidate AS (
    SELECT t.id
    FROM tasks t
    WHERE t.status = 'pending'
      AND t.runnable = true
    ORDER BY t.priority DESC, t.created_at ASC
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
UPDATE tasks
SET status = 'claimed',
    assigned_worker_id = :worker_id,
    assigned_node_id = :node_id,
    lock_expires_at = NOW() + INTERVAL '30 seconds',
    generation = generation + 1,
    claimed_by_cp_id = :control_plane_node_id,
    claimed_at = NOW(),
    updated_at = NOW()
FROM candidate
WHERE tasks.id = candidate.id
RETURNING tasks.id, tasks.generation, tasks.task_type, tasks.context_ref;
```

**Key Invariant:** No two control-plane nodes can ever receive the same candidate task. `SKIP LOCKED` instructs PostgreSQL to bypass any row currently locked by another active transaction.

### 2.3 Generation Fencing

Every subsequent state update (checkpoint creation, task status transition, error reporting, completion) checks the exact generation:

```sql
UPDATE tasks
SET status = :next_status,
    generation = generation + 1,
    result = :result_payload,
    completed_at = NOW(),
    updated_at = NOW()
WHERE id = :task_id
  AND generation = :expected_generation;
```

If `rowcount == 0`:
1. The transaction rolls back.
2. The control plane raises `StaleGenerationConflictError`.
3. The event log records `task.conflict_detected` with forensic payloads.

### 2.4 Distributed Advisory Locks for Singleton Sweeps

Supervisory jobs (e.g. node reaper, orphan recovery, telemetry rollups) must not run concurrently across all nodes:

```python
# Transaction-level advisory lock (automatically released on commit/rollback)
ADVISORY_LOCK_ORPHAN_REAPER = 100_001
ADVISORY_LOCK_NODE_SUPERVISOR = 100_002

async with session.begin():
    result = await session.execute(
        text("SELECT pg_try_advisory_xact_lock(:lock_id)"),
        {"lock_id": ADVISORY_LOCK_ORPHAN_REAPER}
    )
    acquired = result.scalar_one()
    if not acquired:
        logger.debug("Another control plane node is executing orphan recovery sweep; skipping.")
        return
    # Perform orphan recovery logic safely
```

---

## 3. Threat Model & Trust Boundaries

| STRIDE Category | Threat Description | Attack Vector / Impact | PersonaOS Defense & Mitigation |
|---|---|---|---|
| **Spoofing** | Rogue control plane instance connects to DB or API | An attacker spins up unauthorized CP node, reads sensitive task inputs. | PostgreSQL TLS client certificates (mTLS); internal API network segment; mutual HMAC authentication for CP clustering. |
| **Tampering** | Stale or partitioned CP node commits outdated task state | Network partition heals; old CP node attempts to complete a task that was already reclaimed. | Monotonic generation fencing (`generation = :expected_generation`). Stale updates affect 0 rows and abort. |
| **Repudiation** | CP node claims or modifies tasks without trace | Operator cannot determine which CP node dispatched or modified a task. | Append-only `task_events` recording `claimed_by_cp_id`, `actor_id`, and cryptographic hash chaining. |
| **Information Disclosure** | Cross-tenant memory or task context leak | CP node routes task context to improper worker or logging sink. | Context assembly enforces strict 5-scope isolation (ADR 0022/0025); secrets manager redacts sensitive keys recursively. |
| **Denial of Service** | CP node exhaustion / lock hogging | Stuck CP transaction holds row locks, starving other scheduler instances. | Statement timeouts (`statement_timeout = '5s'`), connection pooling quotas, and lock wait limits. |
| **Elevation of Privilege** | Worker node requests administrative API endpoints | Worker node invokes control-plane approval bypass. | Strict RBAC: Worker daemons only present `TaskAccessToken` scoped to `checkpoint:write` and `heartbeat:emit`. |

---

## 4. Phased Implementation Roadmap & Acceptance Criteria

### Milestone v0.2-A.1: Control-Plane Node Registry & Advisory Locking
- **Deliverables:**
  - Alembic Migration 0007 (`control_plane_nodes` table).
  - `ControlPlaneRegistry` managing startup registration, heartbeat ping (`every 5s`), and graceful drain unregistration.
  - PostgreSQL advisory lock context managers (`async with advisory_lock(...)`).
- **Acceptance Criteria:**
  - Multiple CP processes register distinct IDs in `control_plane_nodes`.
  - Terminated CP process is marked `offline` after 3 missed heartbeats (`15s`).
  - Concurrent advisory lock sweep runs on exactly one instance; other instances skip cleanly without exception.

### Milestone v0.2-A.2: Active-Active Scheduler Claim Engine
- **Deliverables:**
  - Refactored `WorkloadScheduler` utilizing `FOR UPDATE SKIP LOCKED`.
  - PgBouncer compatibility (transaction pooling mode support, no session-state dependencies).
- **Acceptance Criteria:**
  - 5 concurrent scheduler instances claiming against a backlog of 200 tasks process every task exactly once with zero deadlocks and zero duplicate assignments.

### Milestone v0.2-A.3: Partition Tolerance & Distributed Recovery
- **Deliverables:**
  - Distributed `NodeSupervisor` and `OrphanTaskReaper` using generation fencing.
  - REST API endpoint for cluster membership status (`GET /api/v1/cluster/nodes`).
- **Acceptance Criteria:**
  - Killing a CP instance mid-claim allows remaining CP instances to immediately recover and re-dispatch tasks once lease expires.

---

## 5. Multi-Node Test Strategy

```
                                  Test Scenarios
┌───────────────────────────────────────┬───────────────────────────────────────────┐
│ Scenario                              │ Verification Method                       │
├───────────────────────────────────────┼───────────────────────────────────────────┤
│ 1. 10-Way Concurrent Claim Race       │ 10 async scheduler tasks competing for    │
│                                       │ 100 runnable tasks in PostgreSQL.         │
│                                       │ Assert len(claimed) == 100, duplicates==0.│
├───────────────────────────────────────┼───────────────────────────────────────────┤
│ 2. Control-Plane Node Sudden Death    │ Spawn CP-1 and CP-2. CP-1 claims Task-X   │
│                                       │ and is SIGKILLed. Advance mock clock past │
│                                       │ lease TTL. Assert CP-2 reclaims Task-X    │
│                                       │ with generation incremented.              │
├───────────────────────────────────────┼───────────────────────────────────────────┤
│ 3. Split-Brain Stale Generation Write │ CP-1 holds Task-X (gen: 1). Network lag   │
│                                       │ causes lease expiry. CP-2 reclaims (gen:2)│
│                                       │ CP-1 attempts completion with gen: 1.     │
│                                       │ Assert CP-1 fails with StaleGeneration;   │
│                                       │ CP-2 state preserved.                     │
└───────────────────────────────────────┴───────────────────────────────────────────┘
```

---

## 6. Open Architectural Decisions Requiring User Approval

1. **PgBouncer Pooling Mode:**
   - *Recommendation:* Adopt `transaction` pooling mode for maximum scalability.
   - *Consequence:* Requires all advisory locks to use `pg_try_advisory_xact_lock` (transaction-scoped) rather than `pg_try_advisory_lock` (session-scoped).
2. **Cluster Heartbeat Cadence:**
   - *Recommendation:* Control plane heartbeat interval of `5.0s`, stale timeout of `15.0s`.
   - *Consequence:* Minimal database write load (~0.2 QPS per CP instance) while detecting crashed instances within 15 seconds.
3. **Internal mTLS CA Deployment:**
   - *Recommendation:* Use internal HashiCorp Vault or cert-manager generated PKI for mutual authentication between CP nodes and load balancers.
