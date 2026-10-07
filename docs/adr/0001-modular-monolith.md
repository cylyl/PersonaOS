# ADR 0001 — Modular monolith, not microservices

**Status:** Accepted (2026-10-07)
**Scope:** PersonaOS v0.1

## Context

v0.1 has seven primitives + security to ship in 4–6 months. Microservices add deployment, observability, inter-service auth, and version-skew overhead that doesn't pay back at this scale.

The README's §11 stack already calls for a "modular monolith with clear interfaces."

## Decision

PersonaOS v0.1 is a **single Python package** (`src/personaos/`) with clear module boundaries:

- `domain/` — pure entities, zero I/O
- `registry/` — worker + node registration
- `workload/` — queue, scheduler, leases
- `security/` — policy, permissions, tool policy, approval, secrets, quarantine
- `execution/` — runner, checkpoint, adapters
- `api/` — FastAPI control plane
- `db/` — session, models, migrations

Internal APIs are **Python function calls**, not HTTP. Two OS processes run on the same box: the API (`uvicorn`) and the worker (`worker_main.py`).

## Consequences

**Positive:**
- One process to debug, one set of logs, one DB, one test harness.
- Module boundaries make future extraction mechanical.
- Tests are simpler (no inter-service mocks).
- The runner can call `security.evaluate(...)` directly without serializing across a wire.

**Negative:**
- Cannot scale API and worker independently until we split them.
- Single Python import graph means a syntax error in one module blocks all modules.
- Module boundaries are conventions, not enforced — code review must enforce them.

**Migration path:** When v0.2 or later needs to scale a subsystem independently (e.g., the worker pool needs more processes than the API), extract that package into its own service. The interface stays the same; only the transport changes.
