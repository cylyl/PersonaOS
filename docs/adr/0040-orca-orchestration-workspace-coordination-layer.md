# ADR 0040 — Orca Orchestration as Workspace Coordination Layer

**Status:** Accepted (2026-11-01)  
**Scope:** PersonaOS Core, PAB Control Plane, Orca ADE Integration, `personaos-orca-plugin`  
**Extends:** ADR 0037 (PersonaOS–Orca Execution Bridge), ADR 0038 (Three-Tier Unified Agent Architecture), ADR 0039 (Versioned PAB ↔ PersonaOS Execution & Lifecycle Protocol v1)  

---

## 1. Context

[ADR 0038](0038-three-tier-agent-architecture.md) established the foundational division of responsibility:
> **"PAB is an agent manager, not an AI agent. PersonaOS is the workforce brain. Orca is the visual workspace."**

Orca (v1.4.245+) introduced **Orca CLI Orchestration** (`orca orchestration`), a structured multi-agent coordination layer featuring:
- **Runs** (`run-create`, `run-use`): Durable session namespaces and FIFO coordinator mailboxes.
- **Task DAGs & Ready Waves** (`task-create`, `task-list`): Dependency graphs (`--deps`) and ready-wave queries (`--ready`).
- **Supervised Dispatches** (`worker-start`, `dispatch`): Authoritative execution attempts, terminal supervision, and process accounting.
- **Typed Inter-Agent Messaging** (`send`, `check`, `reply`): FIFO message batches (`heartbeat`, `ask`, `reply`, `escalation`, `worker_done`).
- **Decision Gates** (`gate-create`, `gate-resolve`): Blocking gates requiring operator decisions.

### Problem Statement

Integrating Orca CLI Orchestration naively risks introducing a **second competing workforce kernel and execution control plane**, causing split-brain authority:
1. If Orca independently decides task readiness and triggers dispatches, PersonaOS's scheduling policies, profile snapshot versioning (ADR 0008, ADR 0009), and generation fencing (ADR 0018) are bypassed.
2. If Orca supervises provider processes directly, PAB's execution supervision (ADR 0038, ADR 0039), session handoff, and normalized event streams are bypassed.
3. If Orca terminal liveness is equated to task liveness, or if Orca `worker_done` settles tasks directly, PersonaOS's atomic task lease kernel, review queue, and attestation policies are corrupted.

---

## 2. Decision

We establish that **Orca CLI Orchestration is integrated strictly as a workspace coordination, visualization, and interaction layer**. It shall **NOT** become an authoritative task queue, scheduler, or execution supervisor.

```
┌────────────────────────────────────────────────────────────────────────┐
│                               ORCA ADE                                 │
│          Visual DAG · Runs · Terminals · Worktrees · Ask/Reply         │
│          Decision Gate Presentation · Native Session Transcripts       │
│                  (WORKSPACE & INTERACTION LAYER)                       │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                   PERSONAOS ORCA PLUGIN (TypeScript)                   │
│           Interactive Orchestration Panel & Bridge Client              │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ [REST API / WebSockets]
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                         PERSONAOS CORE (BRAIN)                         │
│   Worker Identity (Profiles v1..vN) · Durable PostgreSQL Task Queue   │
│   DAG Dependencies · Readiness Engine · Generation Fencing (ADR 0018)  │
│   Task Review Service · Memory Graph · Node Lifecycle Supervisor       │
│              (AUTHORITATIVE WORKFORCE STATE OWNER)                     │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ [PAB v1 Execution Protocol - ADR 0039]
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                       PAB (AGENT CONTROL PLANE)                        │
│    CLI Discovery · Argument Compilation · Execution Supervision        │
│    Open Agent Skills Translation · Monotonic Event Stream              │
│                 (PROVIDER EXECUTION SUPERVISOR)                        │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ [Subprocess Exec]
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        EXTERNAL AGENT RUNTIMES                         │
│           Codex · Claude · AGY · OpenClaw · MiniMax · DSH · Pi         │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Strict Ownership Boundaries

| Capability | PersonaOS Core | PAB Control Plane | Orca ADE |
| :--- | :---: | :---: | :---: |
| **Worker Identity & Profiles** | **Authoritative Owner** | Consumer | Consumer / Presentation |
| **DAG Dependencies & Readiness** | **Authoritative Owner** | N/A | Visualization / Edit Interface |
| **Task Queue & Leases** | **Authoritative Owner** | Lease Consumer | Non-authoritative reference |
| **Task Review & Approvals** | **Authoritative Owner** | N/A | Presentation (Decision Gates) |
| **Provider Process Supervision** | N/A | **Authoritative Owner** (`pab_managed`) | Host/Virtualization (`orca_terminal`) |
| **Git Worktrees & Branch Lifecycle** | Consumer | Consumer | **Authoritative Owner** |
| **Terminal PTY Virtualization** | Consumer | Consumer | **Authoritative Owner** |
| **Inter-Agent / Operator TUI** | Audit Log Consumer | Event Source | **Communication Interface** |

---

## 4. Architectural Rules & Invariants

### 4.1 Partitioned Identifiers and Generations
To prevent authority confusion, all identifiers are strictly partitioned across layers:
- `run_id`: Native Orca coordination run namespace.
- `personaos_task_id`: Authoritative workforce task UUID in PostgreSQL.
- `worker_id`: Persistent AI worker identity (e.g. `backend-dev`).
- `execution_id`: PAB execution supervision lease UUID.
- `dispatch_id`: External Orca dispatch reference.
- `terminal_id`: Opaque PTY / terminal handle.
- `task_generation`: PersonaOS monotonic task lease fencing counter.
- `run_generation`: Orca coordinator leadership generation.

> **Invariant 1 (Generation Non-Interchangeability):**  
> `task_generation` and `run_generation` are independent and never interchangeable. An Orca coordinator generation cannot authorize an old PersonaOS execution to write task results, nor can an Orca terminal handle replace a PersonaOS task lease.

### 4.2 Explicit Process-Lifecycle Ownership
Every task execution must select exactly one process-lifecycle owner:
- **`pab_managed` (Standard)**: PAB owns provider process lifecycle (spawn, monitor, stop, handoff). Orca supplies the workspace context and visual interaction.
- **`orca_terminal`**: Orca owns the terminal/process lifecycle. PAB tracks the execution via the adapter.

Neither system may terminate, restart, or reap processes owned by the other.

### 4.3 Dual Integration Workflows
1. **Path A (PersonaOS-Initiated)**: Workflows composed in Orca ADE submit to PersonaOS (`POST /tasks`). PersonaOS evaluates DAG readiness, leases tasks, and dispatches via PAB. Orca projects execution state onto the visual canvas.
2. **Path B (Orca CLI-Initiated)**: A CLI coordinator creates a task targeting `--agent personaos:<worker_id>`. This routes through the PersonaOS integration endpoint. PersonaOS validates worker capabilities and dispatches through PAB. Orca is never granted permission to spawn unmanaged agent binaries (`claude`, `codex`, `agy`) directly.

### 4.4 Review Sovereignty via Decision Gates
- An Orca Decision Gate (`gate-create`) mirrors a PersonaOS task in `review` status.
- Gate resolution in Orca ADE triggers `TaskReviewService.approve()` or `.reject()` with explicit `expected_generation` and operator ID.
- Orca cannot independently approve tasks or bypass review policies.

### 4.5 Execution Liveness vs. Terminal Liveness
- Terminal presence in Orca is not proof of agent execution health.
- Only authenticated, generation-fenced heartbeats from the execution process renew PersonaOS task leases.
- `worker_done` is an execution result notification. PersonaOS evaluates the result and determines whether settlement or review is required.

---

## 5. Consequences

### Positive
- Preserves the completed, validated M0–M10 core architecture without regressions.
- Eliminates split-brain execution ownership between Orca and PAB.
- Empowers Orca ADE users with rich multi-agent DAG visualization and interactive Decision Gates while preserving enterprise governance and auditability.

### Neutral / Operational
- Requires Orca CLI integration client in `src/personaos/integrations/orca/client.py` to support orchestration subcommands (`run-create`, `task-create`, `gate-create`, `send`, `check`, `reply`).
- Requires mapping layer translating Orca external references to PersonaOS UUIDs and PAB execution IDs.
