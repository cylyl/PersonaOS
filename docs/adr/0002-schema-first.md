# ADR 0002 — Schemas first, code second

**Status:** Accepted (2026-10-07)
**Scope:** PersonaOS v0.1

## Context

v0.1 has five schemas: `worker`, `node`, `task`, `event`, `policy`. The durability boundary lives in the data model. Defining schemas as code (pydantic classes) leads to drift between docs and reality — the schema becomes "whatever the Python class says today," and the README/docs can't be trusted.

The README §12 makes schema definition the first Phase 1 deliverable.

## Decision

All v0.1 schemas live in `schemas/*.yaml` as the **source of truth**. Pydantic models are generated from the YAML (single source, free IDE support). Alembic migrations are derived from schema diffs.

```
schemas/worker.yaml   ─┐
schemas/node.yaml     ─┤
schemas/task.yaml     ─┼─→ codegen ─→ src/personaos/db/models.py
schemas/event.yaml    ─┤                 src/personaos/domain/*.py
schemas/policy.yaml   ─┘
```

## Consequences

**Positive:**
- Schema changes go through a reviewable diff (the YAML).
- Validation logic is centralized, not scattered across the codebase.
- Editor autocompletion for all schema fields (generated pydantic).
- The `events` table column constraint can be regenerated from the YAML.
- Documentation matches reality — `schemas/*.yaml` IS the doc.

**Negative:**
- Codegen step adds build complexity. v0.1 defers the codegen to a `scripts/codegen/` step (manual run); v0.2 may wire it into CI.
- Two languages (YAML + Python) means contributors need to know both.
- Schema evolution requires explicit versioning (v1, v2 in the YAML filename or field).

**Migration path:** If the schema needs a richer model than JSON Schema allows (e.g., conditional fields, custom validators), drop down to pydantic for that schema. YAML remains the canonical doc; pydantic is the runtime type.
