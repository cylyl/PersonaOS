"""Node entity — execution location.

Per ADR 0003: Node is execution location. Worker is identity. Replaceable.

TODO(v0.1): define Node dataclass with id, host, capabilities, status,
joined_at, last_heartbeat_at, current_worker_count, max_worker_count,
runtime_version, drained_at.
Source of truth: schemas/node.yaml.
"""
