# PersonaOS × PAB Integration Guide

**PersonaOS Agent Bridge (PAB)** provides loosely coupled profile and skill synchronization from PersonaOS to external agent execution platforms: **OpenClaw**, **Google Antigravity CLI (`agy`)**, **OpenAI Codex CLI**, **Anthropic Claude Code**, and **MiniMax Coding (`mcode`)**.

---

## 1. Architectural Model

```
   ┌────────────────────────────────────────────────────────┐
   │             PersonaOS Control Plane (:8000)            │
   │  Authoritative Profiles: profiles/{worker_id}/v{N}.yaml │
   │  Authoritative State: PostgreSQL 16                     │
   └───────────────────────────┬────────────────────────────┘
                               │ (asyncio.create_subprocess_exec)
                               ▼
   ┌────────────────────────────────────────────────────────┐
   │             PAB CLI (/root/.local/bin/pab)             │
   │  Reconciliation & Open Agent Skill Distribution Engine │
   └───────┬──────────────┬─────────────┬─────────────┬─────┘
           │              │             │             │
           ▼              ▼             ▼             ▼
      OpenClaw       Antigravity      Codex        Claude & MiniMax
```

### Safety Principles
1. **Source of Truth:** PersonaOS PostgreSQL and immutable profile manifests (`v{N}.yaml`) are the single source of truth.
2. **Subprocess Isolation:** PersonaOS core never imports `pab`. It interacts solely through argument arrays.
3. **Decoupled Lifecycle:** External sync failure never prevents worker profile activation in PersonaOS.
4. **Secret Isolation:** API keys and credentials are never exposed via CLI parameters.

---

## 2. Configuration & Environment Variables

| Variable | Description | Default |
| :--- | :--- | :--- |
| `PAB_BIN` | Path to PAB executable | `/root/.local/bin/pab` (or system `PATH`) |
| `PAB_HOME` | PAB data directory & SQLite cache | `~/.pab` |
| `PERSONAOS_PROFILES_DIR` | Base directory containing worker profiles | `profiles` |

---

## 3. REST API Usage

### Check Bridge Provider Readiness
```bash
curl -X GET http://localhost:8000/workers/bridge/readiness
```

**Response:**
```json
{
  "available": true,
  "providers": {
    "openclaw": {
      "display_name": "OpenClaw",
      "detected": true,
      "version": "OpenClaw 2026.10.6 (fe06181)"
    },
    "agy": {
      "display_name": "Google Antigravity CLI (agy)",
      "detected": true,
      "version": "1.4.11"
    },
    "codex": {
      "display_name": "OpenAI Codex CLI",
      "detected": true,
      "version": null
    },
    "claude": {
      "display_name": "Anthropic Claude Code",
      "detected": true,
      "version": null
    },
    "minimax": {
      "display_name": "MiniMax Coding (mcode)",
      "detected": true,
      "version": "0.6.13"
    }
  }
}
```

---

### Dry-Run Synchronization Plan
Preview external deployment changes without modifying target provider files:

```bash
curl -X POST http://localhost:8000/workers/se/sync \
  -H "Content-Type: application/json" \
  -d '{
    "dry_run": true,
    "targets": ["openclaw", "agy", "minimax"]
  }'
```

---

### Live Synchronization
Synchronize a worker's active profile and skills to external providers:

```bash
curl -X POST http://localhost:8000/workers/se/sync \
  -H "Content-Type: application/json" \
  -d '{
    "dry_run": false,
    "targets": ["openclaw", "agy", "minimax"]
  }'
```

**Response:**
```json
{
  "operation_id": "sync-8f4b23d910a2",
  "worker_id": "se",
  "profile_version": 1,
  "overall_status": "success",
  "dry_run": false,
  "targets": {
    "openclaw": {
      "provider": "openclaw",
      "status": "success",
      "action": "deployed"
    },
    "agy": {
      "provider": "agy",
      "status": "success",
      "action": "deployed"
    },
    "minimax": {
      "provider": "minimax",
      "status": "success",
      "action": "deployed"
    }
  },
  "exit_code": 0
}
```

---

### Automatic Synchronization on Profile Activation
When creating an activated profile version (`POST /workers/{id}/profiles` with `"activate": true`) or activating an existing version (`POST /workers/{id}/activate-profile/{version}`), PersonaOS automatically dispatches a non-blocking background sync task.

If external providers are offline, the database update completes successfully without interruption.

---

## 4. Troubleshooting & Verification

### Verify PAB CLI Installation
```bash
pab --version
pab providers list
```

### Monotonic Version Errors (HTTP 400)
Attempting to synchronize a profile version older than the worker's current active version results in:
```json
{
  "detail": "Cannot sync stale profile version 1; current active version is 2."
}
```
*Resolution:* Supply `dry_run: true` if you wish to inspect historical profiles, or synchronize the latest active version.

### Missing Executable (HTTP 200 with UNAVAILABLE Status)
If PAB is not installed or `PAB_BIN` is invalid, the API returns:
```json
{
  "overall_status": "unavailable",
  "stderr_summary": "PAB binary '/root/.local/bin/pab' is unavailable or not executable."
}
```
*Resolution:* Install PAB via `pip install -e /root/.openclaw/workspace/projects/pab` or ensure `pab` is in `$PATH`.
