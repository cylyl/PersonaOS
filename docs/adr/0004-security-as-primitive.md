# ADR 0004 — Security is a primitive, not a feature

**Status:** Accepted (2026-10-07)
**Scope:** PersonaOS v0.1

## Context

PersonaOS controls persistent AI workers' identity, memory (later), skills (later), tools, secrets, workload, and execution. Without security as a core primitive, the system becomes trivially abusable — a worker reads a production database password, exfiltrates customer data, or runs an unapproved command.

The README's Security appendix makes security a Phase 1 primitive. This ADR locks that decision and defines what v0.1 actually ships.

## Decision

Security surrounds every action in the runner's main loop:

1. **Before execution** — `security.evaluate()` decides allow / deny / require-approval based on policy.
2. **During execution** — `security/secrets.py` resolves secret references; `security/tool_policy.py` enforces tool allow/deny.
3. **After execution** — `security/audit.py` logs every action (already covered by the event log).

### v0.1 security modules (the 8 MVP items)

| Module | Responsibility |
|---|---|
| `security/policy.py` | Policy entity + evaluation engine |
| `security/permissions.py` | RBAC / authority resolution |
| `security/tool_policy.py` | Tool allow/deny enforcement |
| `security/approval.py` | Human approval gates |
| `security/secrets.py` | Secret references + isolation |
| `security/quarantine.py` | Worker kill / quarantine |

Every task passes through `security.evaluate()` before execution. Deny = task fails with reason. Require-approval = task pauses until a human approves. Allow = proceeds.

### Explicitly deferred (out of v0.1)

Each of these is a startup-grade effort on its own:

- DLP for AI interactions
- AI gateway / prompt-injection detection
- Shadow-AI detection
- SIEM integration
- Anomaly detection
- AI red teaming
- Compliance controls (SOC 2, ISO 27001, etc.)

These live in the README vision (AI-Native Security §3), not in the v0.1 codebase.

## Consequences

**Positive:**
- Security is on the hot path — every task goes through `security.evaluate()`. Not bolted on.
- A worker can be quarantined at any time; the scheduler respects the flag (no half-states).
- Approval gates pause tasks until a human approves, blocking runaway automation.
- Secret references (not values) flow through the system — workers never see plaintext secrets in their prompt or logs.

**Negative:**
- Every action adds latency (policy evaluation). v0.1 uses simple SQL-backed rules, so the overhead is microseconds, but it exists.
- Policy authoring becomes a separate concern (the `policy.yaml` schema). Operators need to know how to write policies.
- Approval flow is blocking — a queued task waiting for human approval can pile up if no one is watching the approval queue.

**Migration path:** v0.2 may add a dedicated policy language (e.g., Rego, Cedar) behind the `policy.evaluate()` interface. The runner doesn't change; only the engine swaps.
