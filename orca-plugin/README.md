# PersonaOS Workforce Bridge — Orca ADE Plugin

Visual ADE extension connecting [Orca ADE](https://orca.run) workspaces to the persistent AI workforce kernel [PersonaOS](https://github.com/joseph118789-max/PersonaOS) and [PAB](https://github.com/joseph118789-max/pab) control plane.

## Capabilities

- **Workforce Discovery**: Browse persistent AI workers, capabilities, and profile versions (`GET /workers`).
- **Task Delegation with Worktree Context**: Enqueue tasks to PersonaOS with automatic git worktree binding (`repo_id`, branch, path).
- **Orca Orchestration & DAG Studio (ADR 0040)**: Visual DAG workflow projection, dependency-aware task delegation, ready-wave tracking, and interactive Decision Gates.
- **Human-in-the-Loop Review**: Inspect diagnostic context and approve or reject tasks (`GET /tasks/{id}/review-context`, `POST /tasks/{id}/approve`).
- **Zero-Direct-CLI Invariant**: Guarantees no direct external agent CLI processes (`openclaw`, `codex`, `claude`, `agy`) are spawned by the IDE; all workforce dispatching is securely handled by PersonaOS and supervised by PAB.

## Installation in Orca ADE

### Method 1: Git URL (Recommended)
1. In Orca ADE, open **Settings → Plugins**.
2. Click the **`...`** action menu in the top-right.
3. Select **Install plugin** → Choose **Git**.
4. Enter the pinned repository URL with an explicit `#ref` (tag or commit):
   ```
   https://github.com/joseph118789-max/personaos-orca-plugin#v1.1.0
   ```
5. Click **Install**. Review the requested capabilities (`workspace:read`, `notifications:show`) and toggle **Enable**.

### Method 2: Local Path
1. In Orca ADE, open **Settings → Plugins**.
2. Click the top-right menu **`...`** → **Install plugin**.
3. Choose **Local path** and enter the path where this repository was cloned:
   ```
   /root/projects/personaos-orca-plugin
   ```
4. Click **Install** and **Enable**.

## Architecture

```
┌────────────────────────────────────────────────────────┐
│                       ORCA ADE                         │
│       Desktop / Mobile UI / Worktrees / Terminals      │
│               [PersonaOS Orca Plugin]                  │
└───────────────────────────┬────────────────────────────┘
                            │ REST API (/workers, /tasks)
                            ▼
┌────────────────────────────────────────────────────────┐
│                      PERSONAOS                         │
│         Authoritative Workforce Brain & State          │
│       Workers · Profiles v1..vN · PostgreSQL Queue     │
└───────────────────────────┬────────────────────────────┘
                            │ PABExecutionAdapter
                            ▼
┌────────────────────────────────────────────────────────┐
│                         PAB                            │
│          Universal Agent Control Plane (CLI)           │
│   Supervision · Monotonic Events · SQLite State        │
└───────────────────────────┬────────────────────────────┘
                            │ Managed Process Execution
                            ▼
┌────────────────────────────────────────────────────────┐
│                 EXTERNAL AGENT RUNTIMES                │
│    OpenClaw · AGY · MiniMax · DSH · Pi · Codex · Claude │
└────────────────────────────────────────────────────────┘
```
