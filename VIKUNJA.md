# VIKUNJA — PersonaOS v0.1 Scope

The 4–6 month executable scope. **The kernel only.**

> Compute is replaceable; the worker is persistent.

---

## What v0.1 ships

Seven primitives + security surrounding every action:

```
Worker → Task → Scheduler → Node → Runtime (adapter) → Checkpoint → Event
                                    (surrounded by Security)
```

| Primitive | v0.1 scope |
|---|---|
| **Worker** | Persistent identity, profile, authority. Status: active / paused / quarantined / retired. |
| **Task** | Durable unit of work. 8-state lifecycle. |
| **Scheduler** | FIFO + priority + skill-match in SQL. **No AI scheduler.** |
| **Node** | Execution location, separate from Worker. Status + heartbeat. |
| **Runtime** | Adapter Protocol. First concrete adapter: OpenClaw (thin shim). |
| **Checkpoint** | Durable state snapshot per task. Resume after restart. |
| **Event** | Append-only log. **17 event types.** Not event sourcing — state tables are the source of truth. |
| **Security** | Policy engine, RBAC, tool policy, approval, secrets, quarantine. Surrounds every action in the runner main loop. |

---

## What v0.1 does NOT ship

These belong in the architecture vision (the README), **not in the v0.1 codebase**:

| Deferred | Where it lives |
|---|---|
| Memory (working / episodic / semantic / procedural) | README §6. Schemas/events designed so it slots in later without redesign. |
| Skills registry + admission | README §5. Phase 3. |
| Simulation / replay engine | README §10. Phase 4. Needs historical data first. |
| Cluster scheduling, failover, capability-aware routing | README *Cluster Support*. Single control plane + worker processes on one machine in v0.1. Node abstraction is in the data model but only one node in v0.1. |
| LLM router abstraction | PersonOS delegates to adapters. v0.1 ships one adapter (OpenClaw). Future adapters plug in via the Protocol. |
| AI gateway, prompt-injection detection, shadow-AI detection | README *AI-Native Security §3*. Each is a startup-grade effort. Out of v0.1. |
| SIEM, AI red teaming, compliance controls | README *AI-Native Security §3*. Out of v0.1. |
| pgvector | No Memory in v0.1, no vector retrieval. Add when Memory lands. |

---

## Architectural decisions (locked — see `docs/adr/`)

| ADR | Title | One-line | Status |
|---|---|---|---|
| 0001 | Modular monolith, not microservices | One Python package, clear module boundaries; future splits are mechanical. | Implemented |
| 0002 | Schemas first, code second | `schemas/*.yaml` is the source of truth; pydantic generated from YAML. | Implemented |
| 0003 | Worker is identity, Node is execution location | Worker survives model + runtime changes; Node is replaceable. | Implemented |
| 0004 | Security is a primitive, not a feature | Security surrounds every action in the runner main loop. | Implemented (ADR 0020, commit `16e7ed3`) |
| 0005 | Runtime adapters are thin shims | PersonaOS owns domain logic; adapters translate to/from runtime APIs. | Implemented |
| 0006 | Events are first-class data, not infrastructure | Append-only event log; state tables remain source of truth. | Implemented |
| 0007 | Memory adapter Protocol | Five scopes (persona/project/worker/task/knowledge). | Implemented (ADR 0025/0026, commits `cc1b453`, `d678d28`) |
| 0008 | Thin worker six tables & versioning | `workers`, `tasks`, `worker_skills`, `worker_nodes`, `task_events`, `task_checkpoints`. | Implemented |
| 0009 | Worker execution context | Runtime resolves worker profile strictly via snapshot version. | Implemented |
| 0010 | Worker execution kernel | Kernel owns atomic claims, heartbeats, completions, and failure routing. | Implemented |
| 0011 | Task simulation and replay | Deterministic counterfactual simulation without external side effects. | Implemented |
| 0012 | Worker orchestrator | Polling loop driving the kernel with claim-conflict serialization. | Implemented |
| 0013 | Workload runnable semantics | Read-only FIFO dependency-aware query (`next_runnable`). | Implemented |
| 0014 | Task dependency lifecycle | Durable DAG evaluation and progression in PostgreSQL. | Implemented |
| 0015 | Durable workload queue | Atomic state transitions and crash resilience in PostgreSQL. | Implemented |
| 0016 | In-progress task recovery | Lease expiration, generation fencing, and side-effect-guarded quarantine. | Implemented |
| 0017 | Live OpenClaw ACP adapter | JSON-RPC 2.0 stdio framing, token authentication, and ambiguous failure routing. | Implemented (commit `f68ee1c`) |
| 0018 | Task review resolution lifecycle | Atomic approval, manual resolution, rejection, worker affinity, and Migration 0005. | Implemented (commit `f7ae15a`) |
| 0019 | Worker skill & capability matching scheduler | SQL-based skill and node capability task discovery and claim matching. | Implemented (commit `f656ea0`) |
| 0020 | Security policy enforcement | Fail-closed PolicyEngine, ToolPolicy, approval gating, and secrets management. | Implemented (commit `16e7ed3`) |
| 0021 | Checkpoint resumption recovery | Resilient task recovery across compute replacement without duplicate side effects. | Implemented (commit `dd84837`) |
| 0022 | Worker execution context assembly | Assemble snapshot worker profile, upstream artifacts, and memory into context. | Implemented (commit `3de4a85`) |
| 0023 | Worker concurrency capacity | Concurrency limits and capacity gating in scheduler task assignment. | Implemented (commit `cc1b453`) |
| 0024 | Node lifecycle supervision | Heartbeat tracking, dead node detection, and in-progress task eviction. | Implemented (commit `cc1b453`) |
| 0025 | Worker memory subsystem | Five-scope memory architecture (persona/project/worker/task/knowledge) with Mem0. | Implemented (commit `cc1b453`) |
| 0026 | Memory provenance & confidence | Bounded confidence [0.0, 1.0], exponential/linear decay, and threshold filtering. | Implemented (commit `d678d28`) |
| 0027 | Versioned Skill Registry | Multi-format manifest discovery, semver resolution, and immutable bundles. | Implemented (commit `d678d28`) |
| 0028 | Skill validation & security scanning | Fail-closed AST inspection, dangerous import blocking, and symlink defenses. | Implemented (commit `d678d28`) |
| 0029 | Task telemetry quality, latency & cost | Token tracking, multi-model pricing registry, and quality scoring. | Implemented (commit `d678d28`) |
| 0030 | Worker & workload dashboards | REST telemetry endpoints and single-page operator dashboard UI. | Implemented (commit `d678d28`) |
| 0031 | Empirical estimation calibration | Exponential moving average calibration of duration and tokens from history. | Implemented (commit `d678d28`) |
| 0032 | Team assignment & simulation | Counterfactual team assignment and model comparison with Pareto optimization. | Implemented (commit `d678d28`) |
| 0033 | Multi-Node Control-Plane Distribution | Active-active leaderless control planes with PostgreSQL SKIP LOCKED & generation fencing. | Implemented (commit `da4ceb0`) |
| 0034 | Remote Worker Daemon & Heartbeat Distribution | Server-authoritative heartbeats, generation fencing, and side-effect quarantine. | Implemented (commit `f1b745f`) |
| 0035 | Cross-Machine Agent Execution Protocol (PMAEP) | JSON-RPC 2.0 over WSS, HMAC token auth, and real-time checkpoint streaming. | Implemented (commit `eb404a2`) |
| 0035 | Cross-Machine Agent Execution Protocol | JSON-RPC 2.0 over mTLS WebSocket (PMAEP) with ephemeral scoped JWT tokens. | Accepted (v0.2-C) |

---

## The runner main loop (the kernel)

This is what `execution/runner.py` does. Every other module serves this loop.

```python
while True:
    task = scheduler.claim_next(worker_id, lease_ttl=...)     # Task + Scheduler
    if not task:
        sleep(poll_interval)
        continue

    node = registry.get_node_for_worker(worker_id)            # Worker + Node
    worker = registry.get_worker(worker_id)

    decision = security.evaluate(                             # Security surrounds
        actor=worker,
        action=task.action,
        resource=task.target,
        context=task.context,
    )
    if decision == DENY:
        workload.fail(task, reason=decision.reason)
        events.emit("policy.denied", task=task.id)
        continue
    if decision == REQUIRE_APPROVAL:
        approval.request(task)
        continue

    adapter = registry.get_runtime(node)                       # Runtime (adapter)
    checkpoint.load(task.checkpoint_ref)                       # Checkpoint
    result = adapter.execute(task, checkpoint)                 # Runtime

    checkpoint.write(task, result.state)
    workload.complete(task, result=result)
    events.emit("task.completed", task=task.id)                 # Event
```

---

## Local development

```bash
make up           # Start Postgres
make migrate      # Run Alembic migrations
make seed         # Load example workers from examples/
make test         # Run unit + integration tests
make lint         # Run ruff + mypy
make format       # Auto-format
```

---

## Success criteria for v0.1

From the README §14, updated to reflect verified implementations:

- [x] A worker can resume after an engine restart without losing task state. *(Checkpoint & Resumption — ADR 0021, commit `dd84837`)*
- [x] Relevant memory can be retrieved across separate sessions. *(Memory Subsystem & Five Scopes — ADR 0025/0026, commits `cc1b453`, `d678d28`)*
- [x] Tasks cannot silently disappear from the durable queue. *(Postgres SKIP LOCKED — ADR 0015)*
- [x] Failed tasks can be retried without blindly duplicating side effects. *(Idempotent actions + lease TTL — ADR 0016, 0021)*
- [x] Multiple workers can hand work to each other with traceable ownership. *(Worker assignment & review handoffs — ADR 0018, 0019)*
- [x] Monitoring exposes task outcomes, reliability, latency, and cost. *(Telemetry & Dashboards — ADR 0029, 0030, commit `d678d28`)*
- [x] A workload replay can compare at least two team configurations. *(Team Comparison Simulation — ADR 0032, commit `d678d28`)*

---

## Roadmap Status (Vikunja #100–#119)

All 20 roadmap items are fully implemented, verified with tests, and committed. GitHub Project #7 status has been reconciled to **Done** for all items:

| Vikunja # | Title | Phase | Relevant ADR | Primary Implementation | Test Coverage | GH Board |
|---|---|---|---|---|---|---|
| **#100** | Define worker and task schemas | Phase 1 | ADR 0002, 0008 | `schemas/worker.yaml`, `schemas/task.yaml`, domain models | `test_schemas.py`, `test_migration.py` | **Done** |
| **#101** | Persona Registry & versioned profiles | Phase 1 | ADR 0008, 0009 | `src/personaos/registry/profile.py` | `test_worker_profile_versioning` | **Done** |
| **#102** | Durable workload queue & task lifecycle | Phase 1 | ADR 0013–0015 | `src/personaos/workload/queue.py`, `service.py` | `test_durable_workload_queue.py` | **Done** |
| **#103** | Stored execution events & checkpoints | Phase 1 | ADR 0006, 0008, 0021 | `src/personaos/events/store.py`, `task_checkpoints` | `test_events_log_inspection` | **Done** |
| **#104** | Define OpenClaw ACP adapter | Phase 1 | ADR 0005, 0017 | `src/personaos/execution/adapters/openclaw.py` | `test_openclaw_adapter.py` | **Done** |
| **#105** | Working, episodic, semantic, procedural memory | Phase 2 | ADR 0025 | `src/personaos/memory/`, `adapters/mem0.py` | `test_memory_service.py`, `test_mem0_adapter.py` | **Done** |
| **#106** | Context assembly & retrieval policies | Phase 2 | ADR 0022 | `src/personaos/execution/assembler.py` | `test_context_assembler.py`, pipeline tests | **Done** |
| **#107** | Memory provenance & confidence decay | Phase 2 | ADR 0026 | `src/personaos/memory/scopes.py`, decay policies | `test_provenance_confidence.py` | **Done** |
| **#108** | Resume tasks after process restart & failure | Phase 1 | ADR 0016, 0021 | `src/personaos/execution/kernel.py` | `test_persistent_worker_compute_replacement.py` | **Done** |
| **#109** | Test duplicate actions & unsafe retries | Phase 1 | ADR 0016, 0021 | Crash recovery & lease expiration fences | `test_process_crash_recovery.py` (SIGKILL e2e) | **Done** |
| **#110** | Versioned Skill Registry | Phase 3 | ADR 0027 | `src/personaos/registry/skill.py` | `test_skill_registry.py` | **Done** |
| **#111** | Skill security validation & AST scanning | Phase 3 | ADR 0028 | `src/personaos/security/skill_validator.py` | `test_skill_validator.py` | **Done** |
| **#112** | Track task quality, latency, retries, costs | Phase 3 | ADR 0029 | `src/personaos/telemetry/service.py`, `pricing.py` | `test_task_telemetry.py` | **Done** |
| **#113** | Worker & workload dashboards | Phase 3 | ADR 0030 | `src/personaos/api/routes/dashboard.py` | `test_dashboard_routes.py` | **Done** |
| **#114** | Permission boundaries & human approval gates | Phase 1/3 | ADR 0020 | `src/personaos/security/policy.py`, `approval.py` | `test_policy.py`, `test_security_policy.py` | **Done** |
| **#115** | Multiple worker roles & task handoffs | Phase 1 | ADR 0018, 0019 | `src/personaos/workload/scheduler.py`, `review.py` | `test_scheduler.py`, `test_task_review_lifecycle.py` | **Done** |
| **#116** | Dependencies & capacity constraints | Phase 1/2 | ADR 0014, 0023 | DAG evaluator, worker concurrency limits | `test_task_dependency_lifecycle.py`, capacity tests | **Done** |
| **#117** | Replay historical tasks | Phase 1/4 | ADR 0011 | `src/personaos/simulation/` | `test_simulation_adapter.py` | **Done** |
| **#118** | Counterfactual team & model simulation | Phase 4 | ADR 0032 | `src/personaos/simulation/team_comparison.py` | `test_team_comparison.py` | **Done** |
| **#119** | Calibrate estimates against actual execution | Phase 4 | ADR 0031 | `src/personaos/simulation/calibration.py` | `test_calibration.py` | **Done** |

## Phase-1 acceptance test (the killer property)

Per Liang, 2026-10-07 — the property PersonaOS exists to deliver. If v0.1 ships this, PersonaOS ships.

**The end-to-end flow:**

```
1. Create a Worker (DevOps) with skills [kubernetes] + capabilities [kubectl]
2. Create a Task that requires kubernetes + kubectl (deploy FMS to staging)
3. Scheduler matches Worker to Task (skill + permission + availability + node capability + workload)
4. Worker is bound to a Node (worker_nodes row); OpenClaw adapter starts execution
5. Task emits events — task.created, task.claimed, task.checkpointed, task.completed
6. Checkpoint saved to task_checkpoints after each step
7. Worker finishes; result saved to tasks.result
```

**The killer test — what makes "compute is replaceable; the worker is persistent" real:**

```
8.  STOP node mid-task (kill the OpenClaw process / lose the box)
9.  START a different node (same worker_id, different runtime host)
10. LOAD the same worker (workers.status check — still idle/active)
11. LOAD the task checkpoint (tasks.checkpoint_ref → task_checkpoints)
12. RESUME the task from the last checkpoint
       — no manual intervention
       — no lost work
       — no duplicate side effects (idempotent actions)
```

That last step is what makes the README's thesis enforceable. A worker can move hosts without losing its task. The runtime is replaceable; the worker identity and progress survive.

**Six-table scope for v0.1** (per ADR 0008): `workers`, `tasks`, `worker_skills`, `worker_nodes`, `task_events`, `task_checkpoints`. No policies, leases, pending_approvals, or unified events table — those move to v0.2.

---

## Milestone Tracking & Verification Baseline

### Completed Milestones
- **Step 14b (ADR 0017)** — Live OpenClaw ACP runtime adapter, JSON-RPC 2.0 stdio framing, token-file auth, log redaction, cancellation cleanup, and ambiguous in-flight failure quarantine (commit `f68ee1c`).
- **Step 15 (ADR 0018)** — Task review resolution & operator re-entry lifecycle: atomic generation-fenced approval/resolve/reject endpoints, worker affinity preservation, guarded reassignment, forensic diagnostic retention, Bearer operator authentication, and Migration 0005 expanding `task_events.type` check constraint (commit `f7ae15a`).
- **Step 16 (ADR 0019)** — Worker Skill & Capability Matching Scheduler: SQL-based matching of worker skills and node capabilities during task claim, closed gap between worker profiles and task assignment (commit `f656ea0`).
- **Step 17 (ADR 0021)** — Checkpoint Resumption & Compute Replacement: `task_checkpoints` durable preservation across node restarts, structured resumption context injection, and zero duplicate side effects (commit `dd84837`).
- **Step 18 (ADR 0020)** — Security Policy & Tool Policy Enforcement: Fail-closed `PolicyEngine`, declarative YAML rules, `ToolPolicy` capability allowlists, operator approval gate, worker quarantine, and `SecretsManager` recursive redaction (commit `16e7ed3`).
- **Step 19 (ADR 0022)** — Persistent Worker Execution Context Assembly: `ContextAssembler` resolving frozen worker snapshots, upstream dependency artifacts, recent experience, and memory into immutable execution context (commit `3de4a85`).
- **Step 20 (ADR 0023)** — Worker Concurrency & Capacity Gating: `max_concurrent_tasks` enforcement in SQL claims preventing worker overload (commit `cc1b453`).
- **Step 21 (ADR 0024)** — Node Lifecycle Supervision: `NodeSupervisor` heartbeat tracking, automatic dead node unbinding, and in-progress task eviction (commit `cc1b453`).
- **Step 22 (ADR 0025)** — Worker Memory Subsystem: Five-scope memory architecture (persona/project/worker/task/knowledge), `MemoryAdapter` protocol with Mem0 and in-memory test implementations (commit `cc1b453`).
- **Phase 2–4 Workforce Milestones (ADRs 0026–0032, commit `d678d28`)**:
  - **#107 (ADR 0026)**: Memory provenance tracking and bounded confidence `[0.0, 1.0]` with exponential/linear decay policies.
  - **#110 (ADR 0027)**: Versioned Skill Registry with multi-format manifest discovery and immutable bundle sandboxing.
  - **#111 (ADR 0028)**: Fail-closed skill validation, AST scanning, import blocking, and symlink escape defenses.
  - **#112 (ADR 0029)**: Task telemetry tracking latency, retry counts, token usage, multi-model pricing registry, and quality scoring.
  - **#113 (ADR 0030)**: Worker and workload dashboards REST API and single-page operator console UI.
  - **#119 (ADR 0031)**: Empirical estimation calibration against historical telemetry using exponential moving averages.
  - **#118 (ADR 0032)**: Counterfactual team assignment and model comparison simulation with multi-objective optimization.

### Verification Baseline (Commits `d678d28`, `a34f7ac`, `c4c3873`)
- **Unit & E2E Suites:**
  - `tests/unit`: 115 passed, 1 skipped (`test_openclaw_live_execution` gated behind `PERSONAOS_RUN_LIVE_OPENCLAW_TESTS=1`), 0 failed across 116 tests.
  - `tests/e2e`: 3 passed, 0 failed (including real OS process SIGKILL crash recovery and compute replacement).
- **Integration Test Suites (PostgreSQL-backed):**
  - `tests/integration/test_task_review_lifecycle.py`: 14 passed (ADR 0018 review re-entry).
  - `tests/integration/test_security_policy.py`: 8 passed (ADR 0020 security policies).
  - `tests/integration/test_persistent_worker_compute_replacement.py`: 10 passed (ADR 0021 resumption).
  - `tests/integration/test_worker_context_pipeline.py`: 3 passed (ADR 0022 context assembly).
  - `tests/integration/test_worker_runner_concurrency.py`: 2 passed (ADR 0023 capacity).
  - `tests/integration/test_node_lifecycle_supervision.py`: 2 passed (ADR 0024 node eviction).
  - `tests/integration/test_memory_context_enrichment.py`: 4 passed (ADR 0025 memory scopes).
  - `tests/integration/test_scheduler.py`: 15 passed (ADR 0019 skill matching).
  - `tests/integration/test_api.py`: 9 passed.
  - `tests/integration/test_migration.py`: 15 passed (including bidirectional 0005 and 0006 roundtrip data preservation).
  - `tests/integration/test_worker_execution_kernel.py`: 14 passed.
  - `tests/integration/test_in_progress_task_recovery.py`: 17 passed (ADR 0016 lease and fencing).
  - `tests/integration/test_durable_workload_queue.py`: 10 passed, 1 skipped (ADR 0015).
  - `tests/integration/test_profile_versioning.py`: 14 passed (ADR 0009).
  - `tests/integration/test_task_dependency_lifecycle.py`: 10 passed (ADR 0014).
  - `tests/integration/test_task_execution_context.py`: 7 passed (ADR 0009).
  - `tests/integration/test_simulation_replay.py`: 9 passed (ADR 0011).
  - `tests/integration/test_task_simulation.py`: 9 passed (ADR 0011).
  - `tests/integration/test_worker_orchestrator.py`: 9 passed (ADR 0012).
  - `tests/integration/test_worker_runner.py`: 3 passed (ADR 0024).
  - `tests/integration/test_workload_runnable.py`: 8 passed (ADR 0013).
- **Total Test Suite:** 312 collected, 310 passed, 2 skipped, 0 failed.
- **Code Quality Gates:**
  - Strict Mypy: clean (0 errors across 78 source files).
  - Ruff: clean (`All checks passed!`).
  - `git diff --check`: clean (0 whitespace or line-ending errors).

### Resolved Items & Remaining Technical Debt
- **Resolved (Step 16)**: *Scheduler Skill-Matching* implemented in `src/personaos/workload/scheduler.py` per ADR 0019.
- **Resolved (Step 18)**: *Security Policy Engine* implemented in `src/personaos/security/policy.py`, `tool_policy.py`, and `approval.py` per ADR 0020.
- **Resolved (Step 14b / Commit `a34f7ac`)**: *Live OpenClaw Gateway Environment Isolation* gated behind `PERSONAOS_RUN_LIVE_OPENCLAW_TESTS=1`. All 14 routine adapter unit tests run isolated with deterministic mock scripts without touching port 18789.
- **Resolved (Step 21 / Commit `c4c3873`)**: *Compute-Replacement Shutdown Contract Fix*: `WorkerRunner` records `unbind_reason = "graceful_drain"` when stopping with in-flight tasks and `"runner_shutdown"` when idle/unassigned, matching ADR 0024 and Section 2.2 of the Node Lifecycle Supervision specification.
- **Resolved (Step 15 / Debt Item A)**: *Migration 0005 & 0006 Downgrade Remapping*: Bidirectional lossless roundtrip implemented in migrations 0005 and 0006. On upgrade, any previously downgraded rows with `original_type` are restored cleanly back to their native event types (`task.requeued` and security events). Verified by `test_migration_0005_upgrade_downgrade_data_preservation` and `test_migration_0006_upgrade_downgrade_data_preservation`.
- **Resolved (Step 22 / Debt Item B)**: *Production Vector Persistence*: `get_default_memory_adapter()` and `get_default_memory_service()` factories implemented in `src/personaos/memory/service.py` reading `settings.memory_adapter` (`mem0` vs `in_memory`), `settings.mem0_api_key`, `settings.mem0_base_url`, and `settings.memory_min_confidence`. Integrated into `WorkerExecutionKernel` and `worker_main.py`. Verified by 7 comprehensive production persistence tests in `tests/unit/memory/test_production_persistence.py`.
- **Resolved (Step 12a / Recovery Session)**: *Task Recovery Session Race Condition*: Aligned `test_healthy_slow_worker_is_not_reclaimed_prematurely` in `tests/integration/test_in_progress_task_recovery.py` with deterministic atomic claim polling, preventing SQLAlchemy concurrent session transaction closing conflicts under testcontainer load.
- **Outstanding Operational Dependencies:**
  1. **Live External Vector Provider Credentials**: Production deployment with `MEM0` vector storage requires setting `PERSONAOS_MEM0_API_KEY` and reachable endpoint (`https://api.mem0.ai/v1` or self-hosted cluster). In local dev and CI, `in_memory` adapter remains default.
  2. **Live OpenClaw Gateway Execution**: Gated behind `PERSONAOS_RUN_LIVE_OPENCLAW_TESTS=1` with listening agent on port 18789. Live ACP gateway testing attaches to disposable probe tasks and requires valid gateway authentication token in `/root/.openclaw/openclaw.json`.

### Milestone Target: v0.2 Distributed Workforce Architecture (Implemented & Verified)
- **v0.2 Architecture Specifications & Implementations Completed**:
  - **Milestone v0.2-A: Multi-Node Control-Plane Distribution (Commit `da4ceb0`)**:
    - Specification: `docs/specs/multi-node-control-plane-distribution.md`
    - ADR: `docs/adr/0033-multi-node-control-plane-distribution.md`
    - Implementation: Active-active leaderless control-plane instances (`ControlPlaneRegistry`, node heartbeat, draining, reaping), autonomous task claims with PostgreSQL `FOR UPDATE SKIP LOCKED` (`WorkloadService.claim_atomic`), monotonic generation fencing tokens, and transactional advisory locks (`advisory_xact_lock`) for singleton supervisory sweeps.
    - Tests: Unit tests (`tests/unit/control_plane/test_control_plane.py` - 3 passed) and PostgreSQL integration tests (`tests/integration/test_multi_node_control_plane.py` - 4 passed).
  - **Milestone v0.2-B: Remote Worker Daemon & Heartbeat Distribution (Commit `f1b745f`)**:
    - Specification: `docs/specs/remote-worker-daemon-heartbeat-distribution.md`
    - ADR: `docs/adr/0034-remote-worker-daemon-heartbeat-distribution.md`
    - Implementation: Lightweight `src/personaos/worker_daemon/` package (`SystemCapabilityScanner`, `DaemonConfig`, `ProcessSandboxSupervisor`, `HeartbeatAgent`, `StepIdempotency`, `WorkerDaemon`), server-authoritative leases and heartbeats (5s interval, 15s dead timeout, 30s task lease TTL) immune to client clock skew, split-brain elimination via fail-closed termination on stale generation rejection, and side-effect-aware quarantine for non-idempotent steps (`NodeSupervisor.evict_dead_nodes`).
    - Tests: Unit tests (`tests/unit/worker_daemon/test_worker_daemon.py` - 5 passed) and PostgreSQL integration tests (`tests/integration/test_remote_worker_daemon.py` - 4 passed).
  - **Milestone v0.2-C: Cross-Machine Agent Execution Protocol (PMAEP) (Commit `eb404a2`)**:
    - Specification: `docs/specs/cross-machine-agent-execution-protocol.md`
    - ADR: `docs/adr/0035-cross-machine-agent-execution-protocol.md`
    - Implementation: JSON-RPC 2.0 framing over persistent WebSocket (`/api/v1/cluster/ws`) secured with Mutual TLS 1.3 (mTLS), scoped ephemeral JWT `TaskAccessToken` with HMAC-SHA256 (`TaskTokenManager`), W3C trace context correlation, real-time checkpoint streaming, and standardized custom protocol error codes (-32000 to -32099) with fail-closed rejection on stale generation tokens.
    - Tests: Unit tests (`tests/unit/protocol/test_protocol.py` - 6 passed) and PostgreSQL integration tests (`tests/integration/test_protocol_websocket.py` - 1 passed covering all 5 handshake, heartbeat, checkpoint, stale generation rejection, and completion phases).

## After v0.1

| Phase | Adds |
|---|---|
| **Phase 2** | Memory behind `MemoryAdapter` Protocol (Mem0 first); five scopes (persona/project/worker/task/knowledge); pgvector in compose; provenance + dedup. Resume-after-restart + duplicate-action safety. See ADR 0007. |
| **Phase 3** | Skills registry + admission, full observability (dashboards), expanded security (8-item MVP list completed). |
| **Phase 4** | Multi-worker teams, replay engine, calibration against observed outcomes. |
| **Long-term** | Cluster scheduling, failover, AI gateway, SIEM, red teaming. |

---

## Defaults applied (open questions answered)

Since "go" was given without answering the open questions, I made these calls — flag any to change:

1. **Package name.** Repo `personaos/`, Python package `personaos`.
2. **Process boundary.** API + `worker_main.py` as two processes, same box.
3. **Schema validation.** Pydantic-from-YAML codegen deferred to `scripts/codegen/` step; `db/models.py` has a TODO marker.
4. **Events table.** `events(id, type, actor_id, task_id?, payload jsonb, created_at)` with `type` constrained to the 17-event enum.
5. **`security/policy.py`** owns both entity and engine for v0.1. Flagged as tech debt in the module docstring; split in v0.2 if entity needs independent testing.

---


## Phase 2 — Memory (deferred scope, designed now)

Per ADR 0007, Memory sits behind a `MemoryAdapter` Protocol with five scopes. Phase 2 ships Mem0 only; Graphiti joins later when temporal reasoning becomes a real need.

### The five scopes

```
Memory
├── persona       preferences, working style, long-term goals
├── projects      per-project memory (PersonaOS, FMS, etc.)
├── workers       per-worker (CTO, Developer, QA, DevOps)
├── tasks         current / completed / failed
└── knowledge     architecture, decisions, documentation
```

Retrieval filters by scope + category before vector/graph search. **No global RAG.**

### Phase 2 deliverables

- `src/personaos/memory/` package (deferred from v0.1)
- `src/personaos/memory/adapters/mem0.py` — first concrete adapter
- `src/personaos/memory/adapters/native.py` — pgvector-only fallback (for tests; no dedup/conflict)
- `src/personaos/memory/service.py` — orchestrates adapter + scope validation
- `src/personaos/memory/scopes.py` — Scope / Category enums + validation
- `src/personaos/memory/retrieval.py` — context assembly policies per task type
- `src/personaos/memory/provenance.py` — source, confidence, decay
- pgvector added back to `docker-compose.yml`
- Worker runner integration: `await memory.remember(...)` after successful task, `await memory.recall(...)` before `adapter.execute()`
- Tests: per-scope isolation, dedup behavior, recall precision, scope-leak prevention

### What Phase 2 does NOT add

- Graphiti adapter (Phase 4 when temporal reasoning matters)
- Letta integration (rejected — runtime collision, see ADR 0007)
- Vector DB swap (Qdrant / Weaviate) — pgvector is enough for v0.2
- Memory UI / observability dashboards (Phase 3)

---

## Vikunja is the executable scope. The README is the vision. Don't conflate them.
