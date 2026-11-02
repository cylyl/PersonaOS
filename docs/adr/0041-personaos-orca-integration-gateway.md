# ADR 0041 — PersonaOS–Orca Integration Gateway Architecture

**Status:** Accepted (2026-11-01)  
**Scope:** PersonaOS Integration Layer, Orca ADE Plugin, `personaos-orca-bridge`  
**Extends:** ADR 0037 (PersonaOS–Orca Execution Bridge), ADR 0038 (Three-Tier Unified Agent Architecture), ADR 0040 (Orca Orchestration as Workspace Coordination Layer)  

---

## 1. Context

In Milestone M0 of the Orca Orchestration integration, the visual ADE plugin (`panel/index.html` in `personaos-orca-plugin`) attempted direct browser `fetch()` requests to `http://192.168.1.239:8000/tasks`.

This failed in production with browser-level network errors:
```
Failed to load DAG: Failed to fetch from http://192.168.1.239:8000
Failed to load gates: Failed to fetch from http://192.168.1.239:8000
```

### Root Cause Analysis (Milestone M0 Audit)

1. **Security Context & Mixed Content:** Orca ADE runs in an Electron container or secure web origin (`https://` or `orca-app://`). Modern browser security policies strictly block non-secure plain HTTP (`http://`) requests from secure origins as Mixed Content.
2. **Private Network Access (PNA):** Chromium 104+ restricts web applications from sending requests to private IP ranges (RFC 1918 `192.168.0.0/16`) without strict preflight authorization.
3. **Network Boundary & Routing:** `192.168.1.239` is the private LAN IP of the backend compute node. Client devices (developer laptops, mobile ADE clients, external developer environments) cannot route directly to this IP without a local tunnel or gateway.
4. **Credential Exposure:** Browser-to-API calls expose API tokens in client memory and local storage, violating enterprise zero-trust principles.
5. **Brittle Frontend Data Coupling:** Frontend UI code was forced to perform ad-hoc state aggregation across `/tasks`, `/workers`, and review queues, resulting in fragile error handling.

---

## 2. Decision

We establish the **PersonaOS Integration Gateway / Bridge (`personaos-orca-bridge`)** as a stable integration boundary between Orca ADE and PersonaOS.

> **Key Invariant:**  
> Orca plugin UI panels must **NEVER** issue unmediated direct browser `fetch()` calls to external backend PersonaOS ports. All communication flows through the Integration Gateway.

```
┌────────────────────────────────────────────────────────────────────────┐
│                               ORCA ADE                                 │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │                Orca Plugin UI (panel/index.html)                 │  │
│  │     Workforce · Visual DAG · Ready Waves · Decision Gates        │  │
│  │     Interactive Diagnostics (NETWORK / CORS / AUTH / API)        │  │
│  └──────────────────────────────────┬───────────────────────────────┘  │
│                                     │ [Same-Origin / Local IPC / Bridge]│
│                                     ▼                                  │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │        PersonaOS Orca Integration Gateway (Bridge / BFF)         │  │
│  │  - Connection & Session Management (Endpoint discovery, retries) │  │
│  │  - Auth & Credential Vault (Secure token handling)               │  │
│  │  - Standardized Gateway API (/integration/*)                     │  │
│  │  - SSE Lifecycle Event Relay & Reconnection                      │  │
│  └──────────────────────────────────┬───────────────────────────────┘  │
└─────────────────────────────────────┼──────────────────────────────────┘
                                      │ [Secure API / mTLS / Loopback]
                                      ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        PERSONAOS KERNEL (BRAIN)                        │
│   Worker Identity (Profiles v1..vN) · Durable PostgreSQL Task Queue   │
│   DAG Dependencies · Readiness Engine · Generation Fencing (ADR 0018)  │
│   Task Review Service · Memory Graph · Node Lifecycle Supervisor       │
│                 (AUTHORITATIVE WORKFORCE STATE OWNER)                  │
└─────────────────────────────────────┬──────────────────────────────────┘
                                      │ [PAB v1 Execution Protocol]
                                      ▼
┌────────────────────────────────────────────────────────────────────────┐
│                       PAB (AGENT CONTROL PLANE)                        │
│    CLI Discovery · Argument Compilation · Execution Supervision        │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Deployment Priority for Integration Gateway

| Option | Architecture | Status / Priority | Rationale |
| :--- | :--- | :---: | :--- |
| **Option A** | Orca backend / plugin-host integration | Preferred | If Orca plugin SDK exposes backend HTTP client and host lifecycle hooks. |
| **Option B** | Same-Origin Reverse Proxy / BFF | **Recommended Default** | A lightweight reverse proxy/BFF co-located with Orca ADE runtime. Eliminates CORS and Mixed Content unconditionally. |
| **Option C** | Standalone bridge service on VM | Fallback | Standalone local daemon bridging PersonaOS REST API to local sockets/ports. |
| **Option D** | Direct browser `fetch()` | **Strictly Prohibited** | Fragile, vulnerable to CORS/PNA/Mixed Content, exposes credentials. |

---

## 4. Standardized Integration Gateway API Contract (`/integration/*`)

The Gateway translates raw PersonaOS models into normalized, workspace-ready views without duplicating scheduling or queue logic:

| Route | Method | Description & Contract |
| :--- | :---: | :--- |
| `/integration/health` | `GET` | Health status of Gateway, PersonaOS kernel, and PAB connectivity. |
| `/integration/diagnostics` | `GET` | Deep connection diagnostic report (CORS, network latency, auth, version match). |
| `/integration/workers` | `GET` | Registered persistent workers with skills, capabilities, and active profile versions. |
| `/integration/runs` | `GET`, `POST` | List or create orchestration coordination runs. |
| `/integration/runs/{run_id}/dag` | `GET` | Evaluated DAG projection with task nodes, dependencies, and ready wave states. |
| `/integration/decision-gates` | `GET` | Pending human-in-the-loop review gates mapped directly from tasks in `review` status. |
| `/integration/decision-gates/{gate_id}/resolve` | `POST` | Generation-fenced gate approval/rejection delegating to `TaskReviewService`. |
| `/integration/events` | `GET` (SSE) | Server-Sent Events stream for real-time task progress, heartbeats, and gate transitions. |

---

## 5. Structured Diagnostic Model

The Gateway and plugin UI implement explicit diagnostic error classification rather than generic `Failed to fetch`:

```typescript
export enum ConnectionDiagnosticCode {
  CONNECTED = "CONNECTED",
  NETWORK_UNREACHABLE = "NETWORK_UNREACHABLE",
  CORS_BLOCKED = "CORS_BLOCKED",
  AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED",
  ENDPOINT_NOT_FOUND = "ENDPOINT_NOT_FOUND",
  API_VERSION_MISMATCH = "API_VERSION_MISMATCH",
  INVALID_RESPONSE = "INVALID_RESPONSE",
  SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE",
}
```

---

## 6. Constraints & Safety Invariants

1. **Zero Shadow State:** The Gateway maintains no independent database, task queue, or state machine. It is a stateless translation and connection layer.
2. **Generation Fencing Preservation:** Gate resolution requests MUST carry `expected_generation`. The Gateway forwards this to PersonaOS; any stale generation error (`StaleGenerationConflictError`) is passed back verbatim.
3. **Zero Direct CLI Invariant:** The Gateway and plugin UI NEVER execute external agent binaries (`claude`, `codex`, `agy`). All execution dispatches flow through PersonaOS $\to$ PAB.

---

## 7. Phased Delivery Roadmap (Milestones M0–M7)

- **M0**: Audit current plugin and Orca SDK capabilities; author ADR 0041 and Implementation Specification. *(Complete)*
- **M1**: Define Integration Gateway OpenAPI schema and typed contracts. *(Complete)*
- **M2**: Implement Gateway connection management, token authentication, and health checks. *(Complete)*
- **M3**: Migrate Workforce and Task APIs to Gateway contracts. *(Complete)*
- **M4**: Implement DAG and Ready Waves projection engine. *(Complete)*
- **M5**: Implement Decision Gates and generation-fenced approval delegation. *(Complete)*
- **M6**: Implement SSE lifecycle event streaming with auto-reconnection. *(Complete — Verified on GitHub Milestone 1 / Issue #1)*
- **M7**: End-to-end integration tests across Orca ADE, Gateway, PersonaOS, and PAB. *(Complete — Verified on tests/integration/test_integration_gateway_m7.py)*
