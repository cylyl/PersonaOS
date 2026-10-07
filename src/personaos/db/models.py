"""SQLAlchemy ORM models.

Per ADR 0002: generated from schemas/*.yaml (codegen deferred to
scripts/codegen/ — TODO(v0.2)).

Tables (v0.1):
  workers           — id (PK), name, role, profile (jsonb), authority (jsonb),
                      status, version, created_at, updated_at, last_seen_at,
                      node_id (FK -> nodes.id, nullable)
  nodes             — id (PK), host, capabilities (jsonb), status, joined_at,
                      last_heartbeat_at, current_worker_count, max_worker_count,
                      runtime_version, drained_at
  tasks             — id (PK), type, status, priority, worker_id, node_id,
                      required_skills (jsonb), required_tools (jsonb),
                      payload (jsonb), context (jsonb), dependencies (jsonb),
                      checkpoint_ref, retry_count, max_retries, last_error,
                      deadline (timestamptz), created_at, updated_at,
                      claimed_at, completed_at
  checkpoints       — id (PK), task_id (FK -> tasks.id), ref (unique uuid),
                      state (jsonb), created_at
  policies          — id (PK), name, description, rules (jsonb),
                      default_effect, priority, applies_to (jsonb),
                      created_at, updated_at, version
  leases            — id (PK), task_id (FK), worker_id, node_id, ttl,
                      expires_at, renewed_at
  pending_approvals — id (PK), task_id (FK), policy_id, approvers (jsonb),
                      requested_at, resolved_at, resolved_by, resolution
  events            — id (PK uuid v7), type (CHECK in 17 values, per ADR 0006),
                      actor_id, task_id, worker_id, node_id,
                      payload (jsonb), created_at

TODO(v0.1): hand-define the models (codegen deferred to v0.2). The events.type
column gets a CHECK constraint matching schemas/event.yaml's enum.
"""
