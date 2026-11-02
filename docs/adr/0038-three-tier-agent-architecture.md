# ADR 0038 — Three-Tier Unified Agent Architecture (PersonaOS × PAB × Orca ADE)

**Status:** Accepted (2026-10-31)  
**Scope:** PersonaOS Core, PAB Control Plane, Orca ADE Integration  
**Supersedes/Extends:** ADR 0036, ADR 0037  

---

## Context

Managing heterogeneous AI agent runtimes (OpenAI Codex, Anthropic Claude Code, Google Antigravity CLI, OpenClaw, MiniMax Coding, DeepSeek Shell, Pi) in enterprise environments presents distinct architectural challenges:
1. **Workforce Identity & State**: Persistent workers require long-term memory, versioned capability profiles, atomic task leases, and policy gates that survive individual agent processes or platform restarts.
2. **Process Control & Conversion**: External agent CLIs use divergent configuration formats (`AGENTS.md`, `CLAUDE.md`, TOML, JSON), CLI invocation semantics, and non-portable session transcripts.
3. **User Workspace & Interaction**: Developers require visual desktop and mobile interfaces to view git worktrees, inspect terminals, review code diffs, and approve high-risk actions.

Tightly coupling these concerns into a monolithic application creates unmaintainable software, leaks credentials, and duplicates functionality.

---

## Decision

We establish a **Three-Tier Architecture** with strict responsibility boundaries:

> **"PAB is an agent manager, not an AI agent. PersonaOS is the workforce brain. Orca is the visual workspace."**

```
┌────────────────────────────────────────────────────────┐
│                        ORCA ADE                        │
│          Desktop / Mobile UI / Worktrees / PTYs        │
└───────────────────────────┬────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────┐
│                 PERSONAOS ORCA PLUGIN                  │
│       Worker Selection / Task Views / Approvals        │
└───────────────────────────┬────────────────────────────┘
                            │ [PersonaOS API / WebSockets]
                            ▼
┌────────────────────────────────────────────────────────┐
│                    PERSONAOS CORE                      │
│   Persistent Identity, Profiles, Task State Machine    │
│   Memory Graph, Approvals, Kernel Leases & Fencing     │
│   (AUTHORITATIVE WORKFORCE STATE OWNER — PostgreSQL)   │
└───────────────────────────┬────────────────────────────┘
                            │ [PAB v1 Execution Protocol]
                            ▼
┌────────────────────────────────────────────────────────┐
│               PAB (AGENT CONTROL PLANE)                │
│    CLI Discovery, Process Supervision, Handoffs        │
│    Open Agent Skills Translation, Event Normalization  │
│    (AGENT CONTROL PLANE — SQLite ~/.pab/pab.db)        │
└───────────────────────────┬────────────────────────────┘
                            │ [Subprocess Exec / Hooks]
                            ▼
┌────────────────────────────────────────────────────────┐
│                 EXTERNAL AGENT RUNTIMES                │
│    Codex · Claude · AGY · OpenClaw · MiniMax · DSH     │
└────────────────────────────────────────────────────────┘
```

---

## Boundary Allocation

1. **PersonaOS Core (Brain)**:
   - Owns persistent worker identity (`workers` table), versioned profile manifests (`profiles/{id}/v{N}.yaml`), approved skills, durable memory graph (mem0/vector), atomic task leases, generation fencing, and review gates.
   - Remains the **single source of truth**.
2. **PAB (Control Plane)**:
   - Owns agent runtime discovery, CLI argument compilation, process spawning/monitoring/stopping, cross-provider session handoff (`pab codex --from claude`), Open Agent Skills compilation, and normalized event emission.
   - Must remain **independently executable from CLI** without PersonaOS or Orca.
   - Never acts as a second task queue or workforce kernel.
3. **Orca ADE (Workspace)**:
   - Owns visual presentation across desktop and mobile, Git worktrees, terminal virtualization (PTYs), and native conversation transcripts.
   - Hosts the PersonaOS Orca Plugin. Does not invoke agent CLIs directly.
4. **External Agent CLIs**:
   - Execute the actual engineering turns under PAB supervision.

---

## Consequences

- **Separation of Concerns**: Each system focuses on its core capability without duplicating functionality.
- **Independence**: PAB can be used standalone in terminal workflows; PersonaOS can drive headless CI/CD; Orca operates with or without autonomous workers.
- **Auditability**: Complete end-to-end traceability from PersonaOS task ID $\rightarrow$ PAB execution ID $\rightarrow$ provider native session ID.
