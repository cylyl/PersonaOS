"""Worker entity — persistent identity, profile, authority.

Per ADR 0003: Worker is identity. Survives model + runtime changes.

TODO(v0.1): define Worker dataclass with id, name, role, profile, authority,
status, version, created_at, updated_at, last_seen_at, node_id.
Source of truth: schemas/worker.yaml.
"""
