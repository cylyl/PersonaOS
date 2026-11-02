# ADR 0039 — Versioned PAB ↔ PersonaOS Execution & Lifecycle Protocol (v1)

**Status:** Accepted (2026-10-31)  
**Scope:** PersonaOS Integration Services, PAB Control Plane  
**Target:** Contract Version 1.0  

---

## Context

To execute tasks across heterogeneous agent runtimes (Codex, Claude Code, Google Antigravity, OpenClaw, MiniMax Coding, DeepSeek Shell, Pi), PersonaOS requires a reliable, failure-isolated interface with PAB as the Agent Control Plane.

Direct Python imports between PersonaOS and PAB are strictly prohibited to prevent dependency contamination and maintain out-of-process isolation.

---

## Decision

We establish **Contract Version 1.0** governing the bidirectional protocol between PersonaOS and PAB.

### 1. Distinct Identity Domains
The protocol enforces strict separation across identifiers:
- `worker_id`: PersonaOS persistent worker identifier (e.g. `backend-dev`).
- `worker_profile_version`: Monotonic profile version integer (e.g. `3`).
- `task_id`: PersonaOS task UUID (`task-uuid`).
- `task_generation`: PersonaOS kernel generation fencing counter.
- `execution_id`: PAB execution supervision lease UUID (`pab-exec-uuid`).
- `session_id`: Vendor-native session identifier string (opaque reference).
- `process_id`: OS PID or managed PTY handle.

### 2. Protocol Operations
The protocol defines standard operations:
1. **`list_agents`**: Discovers available runtimes, detected versions, and capability flags (`launch`, `resume`, `stop`, `export_session`, `stream_events`).
2. **`launch`**: Dispatches an authorized task execution request with frozen profile, prompt instructions, approved skills, memory provenance, and target workspace.
3. **`status`**: Queries execution state against normalized lifecycle states (`queued`, `starting`, `running`, `waiting_for_input`, `waiting_for_approval`, `completed`, `failed`, `cancelled`, `interrupted`, `unknown`).
4. **`stream_events`**: Normalizes real-time execution events (`execution.started`, `execution.progress`, `execution.tool_activity`, `execution.waiting_input`, `execution.waiting_approval`, `execution.checkpoint`, `execution.completed`, `execution.failed`).
5. **`stop`**: Issues controlled process termination (SIGINT $\rightarrow$ SIGTERM $\rightarrow$ SIGKILL with timeouts).
6. **`handoff`**: Coordinates technical session migration between source and destination providers, generating a portable `HandoffManifest` (git diff, file hashes, summary, decisions).
7. **`sync_profile` / `sync_skills`**: Translates PersonaOS profiles and skills into external provider formats.

### 3. Failure Isolation & Kernel Authority
- **Non-blocking failure isolation**: If PAB is offline, crashes, or times out, the error is recorded in PersonaOS audit logs, but **never corrupts task state or invalidates active worker profiles in PostgreSQL**.
- **Kernel Fencing**: If a task lease expires and is reassigned by the kernel, any subsequent completion events from an older PAB `execution_id` are rejected via generation fencing (`StaleGenerationConflictError`).
- **Security Sanitization**: Environment variables passed to PAB subprocesses are scrubbed of all secrets, API tokens, and authorization headers.

---

## Consequences

- Deterministic state mapping between proprietary CLI outputs and PersonaOS task states.
- Clean out-of-process execution boundary.
- Zero credential leakage during cross-agent handoffs.
