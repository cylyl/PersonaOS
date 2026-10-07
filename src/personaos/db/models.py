"""SQLAlchemy ORM models.

Per the v0.1 design (Liang, 2026-10-07) + ADR 0008: SIX core tables only.

  workers           — thin identity record (id, role, status, runtime, ...)
  tasks             — unit of work (id, title, objective, status, priority, ...)
  worker_skills     — (worker_id, skill) join table
  worker_nodes      — current/historical binding (worker ↔ node); replaces leases
  task_events       — events related to tasks (subset of all events)
  task_checkpoints  — durable state snapshots for resumable tasks

Deferred from v0.1 (kept here for v0.2+ planning):
  - policies           → v0.1 scheduler uses inline rules; v0.2 if explicit mgmt needed
  - leases             → replaced by worker_nodes binding + task.assigned_worker
  - pending_approvals  → defer human approval gates to v0.2
  - worker / node events (worker.assigned, node.joined, node.heartbeat, ...) → v0.2
  - unified events table → v0.1 ships task_events only; v0.2 if worker/node events needed
  - pgvector           → Phase 2 with Memory (per ADR 0007)

Per ADR 0002: generated from schemas/*.yaml (codegen deferred to v0.2).

TODO(v0.1): hand-define the 6 models below.
task_events.type is CHECK-constrained to the 5 task event types emitted in v0.1:
    task.created, task.claimed, task.checkpointed, task.completed, task.failed
(The other 12 event types from ADR 0006 are documented but not emitted until v0.2.)
"""


def _v01_schema_summary():
    """Document the 6 v0.1 tables here. Implementation in v0.1.

    workers (
        id                  text PRIMARY KEY,
        name                text NOT NULL,
        role                text NOT NULL,
        description         text,
        status              text NOT NULL CHECK (status IN
                              ('idle', 'busy', 'paused', 'quarantined', 'retired')),
        runtime             text NOT NULL DEFAULT 'openclaw',
        max_concurrent_tasks integer NOT NULL DEFAULT 1,
        capabilities        jsonb NOT NULL DEFAULT '[]',
        permissions         jsonb NOT NULL DEFAULT '[]',
        personality         jsonb NOT NULL DEFAULT '{}',
        memory_id           text,                       -- null in v0.1; set in Phase 2
        created_at          timestamptz NOT NULL DEFAULT NOW(),
        updated_at          timestamptz NOT NULL DEFAULT NOW(),
        last_seen_at        timestamptz
    )

    tasks (
        id                   text PRIMARY KEY,
        title                text NOT NULL,
        objective            text NOT NULL,
        status               text NOT NULL CHECK (status IN
                               ('queued', 'assigned', 'in_progress', 'blocked',
                                'review', 'completed', 'failed', 'canceled')),
        priority             text NOT NULL DEFAULT 'medium'
                               CHECK (priority IN ('low', 'medium', 'high', 'critical')),
        type                 text,
        required_skills      jsonb NOT NULL DEFAULT '[]',
        required_capabilities jsonb NOT NULL DEFAULT '[]',
        assigned_worker      text REFERENCES workers(id),
        -- NOTE: no nodes table in v0.1 (per ADR 0008). assigned_node is an opaque
        -- string identifying the runner that owns the task; FK to a future nodes
        -- table when one is introduced (v0.2+ if needed).
        assigned_node        text,
        dependencies         jsonb NOT NULL DEFAULT '[]',
        input                jsonb NOT NULL,
        checkpoint_ref       text,                      -- points to task_checkpoints.ref
        result               jsonb,
        retry_count          integer NOT NULL DEFAULT 0,
        max_retries          integer NOT NULL DEFAULT 3,
        last_error           text,
        deadline             timestamptz,
        created_at           timestamptz NOT NULL DEFAULT NOW(),
        started_at           timestamptz,
        completed_at         timestamptz
    )

    worker_skills (
        worker_id  text NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
        skill      text NOT NULL,
        PRIMARY KEY (worker_id, skill)
    )

    worker_nodes (
        id              text PRIMARY KEY,
        worker_id       text NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
        node_id         text NOT NULL,                  -- opaque runner ID; no FK
        bound_at        timestamptz NOT NULL DEFAULT NOW(),
        unbound_at      timestamptz,                    -- null = current binding
        reason          text,                            -- "assigned", "node_lost", "manual"
        PRIMARY KEY constraint via surrogate id; for v0.1 simplicity allow multiple
        historical rows per (worker, node) pair.
    )

    task_events (
        id           uuid PRIMARY KEY,                  -- uuid v7 (time-sortable)
        task_id      text NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        type         text NOT NULL CHECK (type IN
                       ('task.created', 'task.claimed', 'task.checkpointed',
                        'task.completed', 'task.failed')),
        actor_id     text NOT NULL,                     -- worker or system actor
        payload      jsonb NOT NULL DEFAULT '{}',
        created_at   timestamptz NOT NULL DEFAULT NOW()
    )

    task_checkpoints (
        ref          text PRIMARY KEY,                  -- opaque UUID, surfaced in tasks.checkpoint_ref
        task_id      text NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        state        jsonb NOT NULL,
        last_step    text,
        created_at   timestamptz NOT NULL DEFAULT NOW()
    )

    --- FK notes ---
    - tasks.assigned_worker → workers.id (nullable; queued tasks have no worker)
    - tasks.assigned_node   → nodes.id   (nullable; same)
    - worker_nodes captures binding history (multiple rows per worker over time)
    - task_checkpoints.ref is what tasks.checkpoint_ref points to
    - task_events.type CHECK constraint matches the 5 task types emitted in v0.1
    """
    pass
