# ADR 0037 — PersonaOS–Orca Execution Bridge Architecture

**Status:** Accepted (2026-10-31)  
**Scope:** PersonaOS v0.1+  
**Target:** Integration with Orca (v1.4.245+) as an external execution host

---

## Context

PersonaOS operates on the foundational thesis that **"compute is replaceable; the worker is persistent."** Persistent AI workers maintain long-term identity, role personas, versioned skills, and accumulated memory across arbitrary execution backends (ADR 0003, ADR 0005, ADR 0022).

In modern software development workflows, [Orca](https://www.onorca.dev) provides a host environment for agentic engineering, featuring:
1. Native Git worktree management and branch lifecycle.
2. Multiplexed terminal execution and PTY process supervisor.
3. Native agent CLI runners for Anthropic Claude Code, OpenAI Codex, Google Antigravity (`agy`), and OpenClaw.
4. Native conversation transcript indexing, session discovery, and resume capabilities.

### Problem Statement

Duplicating session history, transcript indexing, terminal virtualization, or worktree provisioning inside PersonaOS would violate architectural separation, duplicate Orca's core strengths, and introduce heavy maintenance overhead. Conversely, Orca lacks persistent worker identities, organizational skill catalogues, cross-agent capability compilation, and atomic task lease kernels.

---

## Decision

We establish the **PersonaOS–Orca Execution Bridge** (`personaos-orca`) as a thin, loosely coupled integration adapter.

```
┌─────────────────────────────────────────────────────────────────┐
│                        PersonaOS Core                           │
│  Worker Registry · Skill Registry · Memory Subsystem · Kernel   │
│  (Authoritative Worker State & PostgreSQL Atomic Task Leases)   │
└───────────────────────────────┬─────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│               PersonaOS Integration Layer                       │
│  Common Contracts: ExecutionRequest, WorktreeDescriptor,        │
│                    AgentDescriptor, ExecutionResult             │
│                                                                 │
│  OrcaExecutionAdapter (src/personaos/integrations/orca/adapter) │
│  OrcaCLIClient (src/personaos/integrations/orca/client)         │
└───────────────────────────────┬─────────────────────────────────┘
                                │ [asyncio.create_subprocess_exec]
                                │ (Argument arrays only, no shell=True)
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                           Orca CLI                              │
│   `orca status --json`                                          │
│   `orca worktree current / list / show --json`                  │
│   `orca terminal create / wait / read / show / close --json`    │
│   `orca agent launch --json`                                    │
└───────────────────────────────┬─────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                     Orca Runtime Host                           │
│  Git Worktrees · Terminals · Claude · Codex · Antigravity       │
│  (Session Transcripts & Interactive Resume Managed by Orca)     │
└─────────────────────────────────────────────────────────────────┘
```

---

## Ownership Boundary Matrix

| Capability | PersonaOS | Orca | Justification |
| :--- | :---: | :---: | :--- |
| **Worker Identity & Persona** | **Owner** | Consumer | Persistent identity survives across environments and models. |
| **Skill Catalogue & Approval** | **Owner** | Installs/Loads | PersonaOS validates and versions skills; Orca exposes them to CLIs. |
| **Cross-Agent Skill Translation** | **Owner** | Consumer | PAB/PersonaOS adapts canonical skills to agent-specific formats. |
| **Long-Term Worker Memory** | **Owner** | Context Source | Memory graphs and provenance are assembled by PersonaOS. |
| **Task Lifecycle & Leases** | **Owner** | Interface | PersonaOS kernel atomically claims, verifies, and settles tasks. |
| **Git Worktrees & Branches** | Consumer | **Owner** | Orca manages git workspaces, worktree isolation, and disk cleanups. |
| **PTY & Terminal Sessions** | Consumer | **Owner** | Orca supervises process lifecycles and I/O multiplexing. |
| **Agent CLI Host (Claude/Codex/Agy)** | Consumer | **Owner** | Orca launches and manages native agent CLI processes. |
| **Session Resume & Transcripts** | Reference Only | **Owner** | Native session IDs remain opaque references; transcripts belong to Orca. |

---

## Core Invariants

1. **Kernel Ownership Invariant:**
   - The PersonaOS kernel (`WorkerExecutionKernel`) retains exclusive ownership of task claiming, atomic database leases, and state machine transitions.
   - The `OrcaExecutionAdapter` cannot transition task records in PostgreSQL or independently settle task claims. It returns normalized `ExecutionResult` records to the kernel.
2. **Native Session Reference Opacity:**
   - Orca session IDs and terminal handles (`native_session_id`, `native_terminal_id`) remain opaque strings.
   - PersonaOS does not attempt to parse, deserialize, or normalize proprietary vendor conversation transcripts.
3. **Zero Shell Injection & Subprocess Security:**
   - All Orca CLI invocations use `asyncio.create_subprocess_exec` with explicit argument arrays. `shell=True` is strictly prohibited.
   - Environment variables passed to Orca CLI subprocesses are scrubbed of credentials, API secrets, and sensitive tokens.
4. **No Direct Database Coupling:**
   - The integration interacts strictly with Orca's public CLI (`orca --json`). PersonaOS never accesses Orca's internal SQLite/state stores directly.
5. **Non-Blocking Execution & Safe Terminal Cleanup:**
   - Managed terminal sessions spawned for task executions are tracked with a monotonic lifecycle and reliably reaped in `finally` blocks via `orca terminal close`.

---

## Integration Contracts (`src/personaos/integrations/common/contracts.py`)

- **`ExecutionRequest`**: Normalized task execution payload carrying worker identity, agent descriptor, worktree target, timeout, and environment metadata.
- **`ExecutionHandle`**: Opaque tracking token encapsulating worktree ID and native session/terminal handles.
- **`WorktreeDescriptor`**: Normalized worktree metadata (`worktree_id`, `instance_id`, `repo_id`, `path`, `branch`, `head`, `display_name`).
- **`AgentDescriptor`**: Agent specification (`kind` e.g. `codex`, `claude`, `agy`, `openclaw`, `custom`, `mock`, plus model, prompt, and options).
- **`ExecutionEvent`**: Lifecycle stream event (`PREPARING`, `RUNNING`, `COMPLETED`, `FAILED`, `TIMEOUT`).
- **`ExecutionResult`**: Immutable execution outcome with exit code, duration, summary, raw terminal preview, and audit evidence.

---

## Consequences

### Positive
- **Clean Separation of Concerns:** PersonaOS remains focused on persistent workers, organizational memory, and tasks; Orca remains the execution runtime.
- **Support for Heterogeneous Agents:** Seamlessly executes tasks across Codex, Claude Code, Antigravity CLI, OpenClaw, and custom commands.
- **Zero Schema Bloat:** No external Orca dependencies in the PersonaOS core database schema.
- **Native Debuggability:** Developers can open Orca UI and directly inspect or resume agent sessions initiated by PersonaOS.

### Negative / Trade-offs
- **Orca Daemon Dependency:** Live execution requires the local Orca runtime daemon (`orcad`) to be operational. When unavailable, the adapter falls back gracefully with structured `OrcaUnavailableError`.
- **PTY Buffering Overhead:** Capturing large terminal outputs requires inspecting Orca's terminal buffer preview when processes exit quickly.
