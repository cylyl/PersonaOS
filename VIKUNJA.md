# VIKUNJA — PersonaOS v0.1 Scope

The 4–6 month executable scope. **The kernel only.**

> Compute is replaceable; the worker is persistent.

---

## What v0.1 ships

Seven primitives + security surrounding every action:

```
Worker → Task → Scheduler → Node → Runtime (adapter) → Checkpoint → Event
                                    (surrounded by Security)
```

| Primitive | v0.1 scope |
|---|---|
| **Worker** | Persistent identity, profile, authority. Status: active / paused / quarantined / retired. |
| **Task** | Durable unit of work. 8-state lifecycle. |
| **Scheduler** | FIFO + priority + skill-match in SQL. **No AI scheduler.** |
| **Node** | Execution location, separate from Worker. Status + heartbeat. |
| **Runtime** | Adapter Protocol. First concrete adapter: OpenClaw (thin shim). |
| **Checkpoint** | Durable state snapshot per task. Resume after restart. |
| **Event** | Append-only log. **17 event types.** Not event sourcing — state tables are the source of truth. |
| **Security** | Policy engine, RBAC, tool policy, approval, secrets, quarantine. Surrounds every action in the runner main loop. |

---

## What v0.1 does NOT ship

These belong in the architecture vision (the README), **not in the v0.1 codebase**:

| Deferred | Where it lives |
|---|---|
| Memory (working / episodic / semantic / procedural) | README §6. Schemas/events designed so it slots in later without redesign. |
| Skills registry + admission | README §5. Phase 3. |
| Simulation / replay engine | README §10. Phase 4. Needs historical data first. |
| Cluster scheduling, failover, capability-aware routing | README *Cluster Support*. Single control plane + worker processes on one machine in v0.1. Node abstraction is in the data model but only one node in v0.1. |
| LLM router abstraction | PersonOS delegates to adapters. v0.1 ships one adapter (OpenClaw). Future adapters plug in via the Protocol. |
| AI gateway, prompt-injection detection, shadow-AI detection | README *AI-Native Security §3*. Each is a startup-grade effort. Out of v0.1. |
| SIEM, AI red teaming, compliance controls | README *AI-Native Security §3*. Out of v0.1. |
| pgvector | No Memory in v0.1, no vector retrieval. Add when Memory lands. |

---

## Architectural decisions (locked — see `docs/adr/`)

| ADR | Title | One-line |
|---|---|---|
| 0001 | Modular monolith, not microservices | One Python package, clear module boundaries; future splits are mechanical. |
| 0002 | Schemas first, code second | `schemas/*.yaml` is the source of truth; pydantic generated from YAML. |
| 0003 | Worker is identity, Node is execution location | Worker survives model + runtime changes; Node is replaceable. |
| 0004 | Security is a primitive, not a feature | Security surrounds every action in the runner main loop. |
| 0005 | Runtime adapters are thin shims | PersonaOS owns domain logic; adapters translate to/from runtime APIs. |
| 0006 | Events are first-class data, not infrastructure | Append-only event log; state tables remain source of truth. |

---

## The runner main loop (the kernel)

This is what `execution/runner.py` does. Every other module serves this loop.

```python
while True:
    task = scheduler.claim_next(worker_id, lease_ttl=...)     # Task + Scheduler
    if not task:
        sleep(poll_interval)
        continue

    node = registry.get_node_for_worker(worker_id)            # Worker + Node
    worker = registry.get_worker(worker_id)

    decision = security.evaluate(                             # Security surrounds
        actor=worker,
        action=task.action,
        resource=task.target,
        context=task.context,
    )
    if decision == DENY:
        workload.fail(task, reason=decision.reason)
        events.emit("policy.denied", task=task.id)
        continue
    if decision == REQUIRE_APPROVAL:
        approval.request(task)
        continue

    adapter = registry.get_runtime(node)                       # Runtime (adapter)
    checkpoint.load(task.checkpoint_ref)                       # Checkpoint
    result = adapter.execute(task, checkpoint)                 # Runtime

    checkpoint.write(task, result.state)
    workload.complete(task, result=result)
    events.emit("task.completed", task=task.id)                 # Event
```

---

## Local development

```bash
make up           # Start Postgres
make migrate      # Run Alembic migrations
make seed         # Load example workers from examples/
make test         # Run unit + integration tests
make lint         # Run ruff + mypy
make format       # Auto-format
```

---

## Success criteria for v0.1

From the README §14, trimmed to what's actually in v0.1:

- [ ] A worker can resume after an engine restart without losing task state. *(Checkpoint)*
- [ ] ~~Relevant memory can be retrieved across separate sessions.~~ *(Phase 2)*
- [ ] Tasks cannot silently disappear from the durable queue. *(Postgres SKIP LOCKED)*
- [ ] Failed tasks can be retried without blindly duplicating side effects. *(Idempotent actions + lease TTL)*
- [ ] Multiple workers can hand work to each other with traceable ownership. *(Worker assignment in event log)*
- [ ] ~~Monitoring exposes task outcomes, reliability, latency, and cost.~~ *(Phase 3 — basic read-only `/events` only in v0.1)*
- [ ] ~~A workload replay can compare at least two team configurations.~~ *(Phase 4)*

---

## After v0.1

| Phase | Adds |
|---|---|
| **Phase 2** | Memory (5 categories), retrieval policies, provenance, resume-after-restart test, duplicate-action safety. |
| **Phase 3** | Skills registry + admission, full observability (dashboards), expanded security (8-item MVP list completed). |
| **Phase 4** | Multi-worker teams, replay engine, calibration against observed outcomes. |
| **Long-term** | Cluster scheduling, failover, AI gateway, SIEM, red teaming. |

---

## Defaults applied (open questions answered)

Since "go" was given without answering the open questions, I made these calls — flag any to change:

1. **Package name.** Repo `personaos/`, Python package `personaos`.
2. **Process boundary.** API + `worker_main.py` as two processes, same box.
3. **Schema validation.** Pydantic-from-YAML codegen deferred to `scripts/codegen/` step; `db/models.py` has a TODO marker.
4. **Events table.** `events(id, type, actor_id, task_id?, payload jsonb, created_at)` with `type` constrained to the 17-event enum.
5. **`security/policy.py`** owns both entity and engine for v0.1. Flagged as tech debt in the module docstring; split in v0.2 if entity needs independent testing.

---


## Vikunja is the executable scope. The README is the vision. Don't conflate them.
