# Integrating Orca CLI Orchestration with PersonaOS
## Architectural Specification & Integration Blueprint (v2 — Aligned with Three-Tier Architecture)

**Target Repository**: `personaos-orca-plugin` (`/root/projects/personaos-orca-plugin`)  
**Core Framework**: [PersonaOS](https://github.com/joseph118789-max/PersonaOS) & [PAB](https://github.com/joseph118789-max/pab)  
**Governing Architecture**: ADR 0038 (Three-Tier Unified Agent Architecture), ADR 0037 (PersonaOS–Orca Execution Bridge), ADR 0039 (PAB–PersonaOS Protocol v1), ADR 0040 (Orca Orchestration as Workspace Coordination Layer)  
**Status**: Revised Specification (Post-Review Alignment)  

---

## 1. Executive Summary & Foundational Boundary

> **Architectural Law (ADR 0038):**  
> *"PAB is an agent manager, not an AI agent. PersonaOS is the workforce brain. Orca is the visual workspace."*

This revision corrects the execution ownership flaws identified in the initial draft:
1. **No Competing Workforce Kernel**: Orca CLI Orchestration is **strictly integrated as a workspace coordination, visualization, and interaction layer**. It does **NOT** become a second workflow engine, scheduler, or execution control plane.
2. **Authoritative Task Lifecycle**: PersonaOS retains sole, authoritative ownership of task records, DAG dependency resolution, ready-wave calculation, leases, generation fencing, and review gates in PostgreSQL.
3. **Execution Supervision**: Provider execution supervision belongs strictly to **PAB (PersonaOS Agent Bridge)**. Orca provides workspace terminals, worktree isolation, and human interaction.

---

## 2. Gap Analysis & Corrective Boundary Matrix

| Area | Initial RFC (Flawed) | Corrected Architecture (Three-Tier Aligned) |
| :--- | :--- | :--- |
| **Run** | Orca Run maps to PersonaOS workflow/campaign | Orca Run is an **external coordination scope** linked to PersonaOS tasks. It is not an authoritative workflow engine. |
| **Task DAG** | Orca creates tasks and dependencies | **PersonaOS stores dependencies and determines readiness**. Orca displays and edits the DAG through PersonaOS APIs. |
| **Ready Waves** | Orca triggers ready tasks | **PersonaOS scheduler and kernel decide which tasks execute**. Orca projects and visualizes the resulting ready waves. |
| **Dispatch** | Orca `worker-start` starts workers directly | Dispatch must pass through **PersonaOS and `PABExecutionAdapter`**. |
| **Process Supervision** | Orca dispatches and supervises terminals | **Single lifecycle owner per execution mode** (`pab_managed` vs `orca_terminal`). Orca and PAB never independently supervise the same process. |
| **Decision Gates** | Orca resolves gates | **Orca presents the gate UX**; PersonaOS remains authoritative for approval, rejection, authorization, and cryptographic audit. |
| **Heartbeats** | Orca heartbeat renews PersonaOS leases | Terminal liveness is not execution liveness. **Only an authenticated, generation-fenced execution heartbeat** from the worker process renews the PersonaOS lease. |
| **Worker Done** | Orca message settles the task | `worker_done` is an **execution result notification**, not an authoritative settlement. PersonaOS validates the result and applies review policy. |
| **Ask/Reply** | Orca messaging handles interaction | Orca provides the **communication interface/TUI**; PersonaOS preserves authorization, durable state, and audit records. |
| **Escalation** | Worker message moves task to quarantine | Routes through **PersonaOS policy and review services**. A raw message cannot mutate protected task state. |

---

## 3. High-Level Target Architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│                               Orca ADE                                 │
│          Visual DAG · Runs · Terminals · Worktrees · Ask/Reply         │
│          Decision Gate Presentation · Native Session Transcripts       │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                   PersonaOS Orca Plugin (TypeScript)                   │
│           Interactive Orchestration Panel & Bridge Client              │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ [REST API / WebSockets]
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                         PersonaOS Core (Brain)                         │
│   Worker Identity (Profiles v1..vN) · Durable PostgreSQL Task Queue   │
│   DAG Dependencies · Readiness Engine · Generation Fencing (ADR 0018)  │
│   Task Review Service · Memory Graph · Node Lifecycle Supervisor       │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ [PAB v1 Execution Protocol - ADR 0039]
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                       PAB (Agent Control Plane)                        │
│    CLI Discovery · Argument Compilation · Execution Supervision        │
│    Open Agent Skills Translation · Monotonic Event Stream              │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ [Subprocess Exec]
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        External Agent Runtimes                         │
│           Codex · Claude · AGY · OpenClaw · MiniMax · DSH · Pi         │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Dual Integration Workflows

### Path A: PersonaOS-Initiated Workflows (Top-Down Composition)
1. **DAG Composition**: The developer visually composes a multi-task workflow in the Orca ADE panel.
2. **Persistence**: The plugin submits the DAG to PersonaOS (`POST /tasks`). PersonaOS stores tasks, worker assignments, and dependency edges.
3. **Scheduling & Claiming**: PersonaOS evaluates DAG readiness. When upstream tasks succeed, dependent tasks become `runnable`.
4. **Dispatch**: PersonaOS dispatches runnable tasks through `PABExecutionAdapter`.
5. **Projection**: Orca receives execution status and projects active nodes, progress, and logs onto the visual workspace.

### Path B: Orca CLI-Initiated Workflows (Coordinator-Assisted)
1. **Coordinator Invocation**: A developer or terminal coordinator runs `orca orchestration run-create` and `task-create`.
2. **Integration Request**: When targeting a PersonaOS worker (`--agent personaos:<worker_id>`), Orca routes the request through a PersonaOS integration endpoint.
3. **Validation & Ownership**: PersonaOS validates the worker ID, checks capability matching, records the task in PostgreSQL, and dispatches via PAB.
4. **Invariant**: `--agent personaos:<worker_id>` is an explicit request for PersonaOS workforce governance—**it does not grant Orca permission to launch raw agent binaries (`claude`, `codex`, `agy`) directly**.

---

## 5. Strict Identifier Separation & Generation Fencing

To prevent split-brain execution and ambiguous authority, all identifiers and generation counters are strictly partitioned:

| Identifier | Owner | Purpose & Boundary |
| :--- | :--- | :--- |
| `run_id` | Orca | External coordination run namespace and coordinator inbox. |
| `personaos_task_id` | PersonaOS | Authoritative workforce task UUID in PostgreSQL. |
| `worker_id` | PersonaOS | Persistent AI worker identity (e.g. `backend-dev`). |
| `execution_id` | PAB | Provider execution instance lease UUID. |
| `dispatch_id` | Orca | External Orca dispatch reference. |
| `terminal_id` | Orca | Opaque PTY / terminal handle. |
| `task_generation` | PersonaOS | **Task fencing counter**; increments monotonically on review/requeue. |
| `run_generation` | Orca | **Coordinator/run generation**; tracks coordinator leadership. |

> [!CAUTION]
> `task_generation` and `run_generation` are **never interchangeable**. An Orca coordinator generation cannot authorize an old PersonaOS execution to write task results, nor can an Orca terminal handle replace a PersonaOS task lease.

---

## 6. Execution Ownership Modes

Every execution must explicitly specify exactly one process-lifecycle owner:

1. **`pab_managed` (Default)**:
   - PAB owns the provider process lifecycle (spawn, monitor, stop, handoff).
   - Orca provides the git worktree and visual interaction.
2. **`orca_terminal`**:
   - Orca owns the terminal/process lifecycle.
   - PAB tracks execution through the defined Orca integration adapter.

Neither system may terminate, restart, or reap processes owned by the other.

---

## 7. Review, Interaction, and Event Contracts

### 7.1 Decision Gates $\leftrightarrow$ PersonaOS Review Queue
- An Orca Decision Gate (`gate-create`) is a **presentation mirror** of a PersonaOS task in `review` status.
- When an operator resolves a gate in Orca (`gate-resolve`), the plugin calls `TaskReviewService.approve()` or `TaskReviewService.reject()` in PersonaOS, passing:
  - `task_id`
  - `expected_generation` (`task_generation`)
  - `operator_id`
  - `review_notes` / `attestation`
- Orca cannot independently approve tasks or bypass review policies.

### 7.2 Ask / Reply
- Orca's `ask` and `reply` commands serve as the human communication interface.
- Questions and answers are recorded in PersonaOS task event streams to maintain an unbroken audit trail.

### 7.3 Multi-Tier Event Semantics
- **Orca Messages**: Human/coordinator interaction and communication workflows.
- **PAB Events**: Provider execution lifecycle, tool activity, and raw progress.
- **PersonaOS Events**: Authoritative task state transitions, lease renewals, and security audits.

---

## 8. Phased Implementation Plan

```
Phase 0: Inspection & Conflict Audit (Repo Inspection, ADR 0040 Drafting)
Phase 1: Safe Orca CLI Client & Capability Probing (tests, typed responses)
Phase 2: Entity Mapping & Fencing Contracts (IDs, generation validation)
Phase 3: PersonaOS-to-Orca State Projection (readiness, progress sync)
Phase 4: Orca-to-PersonaOS Actions (task creation, Ask/Reply, gate resolution)
Phase 5: PAB Execution Supervision Integration (lifecycle modes)
Phase 6: ADE Orchestration UI (DAG visualizer, gate cards, workflow composer)
Phase 7: Reliability & Security Hardening (split-brain tests, crash recovery)
Phase 8: End-to-End Verification & Acceptance Scenarios
```
