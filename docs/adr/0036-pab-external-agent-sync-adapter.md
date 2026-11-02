# ADR 0036 — Loosely Coupled External Agent & Skill Synchronization via PAB

**Status:** Accepted (2026-10-31)  
**Scope:** PersonaOS v0.1+  
**Target:** Integration with `joseph118789-max/pab` (PersonaOS Agent Bridge)

---

## Context

PersonaOS operates on the core principle that **"compute is replaceable; the worker is persistent."** The worker identity, capabilities, permissions, and workload history survive model iterations and engine restarts (ADR 0003, ADR 0005).

In production environments, engineering teams utilize heterogeneous agent platforms:
1. **OpenClaw** — Native multi-agent platform and local workspace runtime.
2. **Google Antigravity CLI (`agy`)** — Progressive disclosure rule and skill engine.
3. **OpenAI Codex CLI** — TOML-configured agent workspaces.
4. **Anthropic Claude Code** — Markdown subagent definitions and tool rules.
5. **MiniMax Coding (`mcode`)** — Terminal coding agent and tool plugin framework.

Without an automated synchronization bridge, maintaining identical agent personalities, system prompts, tool policies, and Open Agent Skills across these runtimes requires error-prone manual duplication.

However, tightly integrating an external bridge into PersonaOS's core process would violate modular boundaries (ADR 0001), create dependency conflicts, and risk compromising PersonaOS's authoritative state.

---

## Decision

We adopt **Option A: Loosely Coupled Subprocess Integration**:

```
PersonaOS Core (PostgreSQL 16)
   │
   ├── ProfileRegistry (profiles/{id}/v{N}.yaml)
   └── API Routes (FastAPI :8000)
          │
          ▼
   PABSyncAdapter (src/personaos/registry/pab_adapter.py)
          │
          │ [Out-of-Process: asyncio.create_subprocess_exec]
          ▼
   PAB CLI (/root/.local/bin/pab)
          │
          ├── pab personaos import <profile_path>
          ├── pab agent deploy <worker_id> --to <target> --json
          └── pab skill deploy <skill_name> --to <target> --json
          │
          ├── OpenClaw (~/.openclaw/openclaw.json)
          ├── Google Antigravity (~/.gemini/config/agents/)
          ├── OpenAI Codex (~/.codex/agents/*.toml)
          ├── Anthropic Claude Code (~/.claude/agents/*.md)
          └── MiniMax Coding (~/.minimax/agents/*/agent.md)
```

### Architectural Invariants

1. **PersonaOS is the Single Source of Truth:**
   - Worker identity (`workers` table) and versioned profile manifests (`profiles/{worker_id}/v{version}.yaml`) are created, validated, and activated solely within PersonaOS.
   - PAB is strictly a downstream compiler and synchronization client.
2. **Zero Direct Python Package Coupling:**
   - PersonaOS never imports `pab` directly (`import pab` is forbidden in PersonaOS core).
   - All interactions execute via `asyncio.create_subprocess_exec` using validated argument arrays (no shell string interpolation).
3. **Non-Blocking Failure Isolation:**
   - External provider synchronization is non-blocking.
   - If PAB is missing, offline, or times out, the failure is logged and recorded in audit metadata, but **never rolls back valid profile creation or activation in PostgreSQL**.
4. **Monotonic Version Guard:**
   - Synchronization validates that the target profile version is greater than or equal to `worker.current_profile_version` to prevent stale profile overwrites.
5. **Strict Credential & Environment Sanitization:**
   - Environment variables passed to PAB subprocesses are scrubbed of all API keys, secrets, tokens, and authorization headers.
   - Credentials remain in secure platform storage and are never passed via CLI flags.

---

## API Surface

1. **`GET /workers/bridge/readiness`**:
   - Queries `pab providers list --json`.
   - Returns discovery and version status for all five supported providers.
2. **`POST /workers/{worker_id}/sync`**:
   - Manually synchronizes a worker profile to selected target providers.
   - Supports `version: int | None`, `targets: list[str] | None`, and `dry_run: bool`.
3. **Automated Activation Hook:**
   - `ProfileRegistry.activate_profile_version()` dispatches a background task (`_dispatch_background_sync`) upon activation, ensuring external runtimes receive active profile updates automatically.

---

## Provider Capability Matrix

| Provider | Profile Sync | Skill Sync | Target Configuration Path |
| :--- | :---: | :---: | :--- |
| **OpenClaw** | ✅ Supported | ✅ Supported | `~/.openclaw/openclaw.json`, `~/.openclaw/plugin-skills/` |
| **Google Antigravity (`agy`)** | ✅ Supported | ✅ Supported | `~/.gemini/config/agents/`, `~/.gemini/config/skills/` |
| **OpenAI Codex CLI** | ✅ Supported | ✅ Supported | `~/.codex/agents/*.toml`, `~/.codex/skills/` |
| **Anthropic Claude Code** | ✅ Supported | ✅ Supported | `~/.claude/agents/*.md`, `~/.claude/skills/` |
| **MiniMax Coding (`mcode`)** | ✅ Supported | ✅ Supported | `~/.minimax/agents/*/agent.md`, `~/.minimax/skills/` |

---

## Consequences

**Positive:**
- Complete independence: PersonaOS functions normally even if PAB is uninstalled.
- Zero package dependency collisions on Python 3.12.
- Multi-runtime agent distribution across 5 heterogeneous platforms from a single PersonaOS profile.
- Monotonic versioning and dry-run safety prevent configuration corruption.

**Negative:**
- Requires the `pab` executable installed on the host machine (`/root/.local/bin/pab`) to perform live sync.
- Subprocess overhead (~0.2s - 0.5s per execution).
