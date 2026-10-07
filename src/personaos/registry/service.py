"""Persona Registry service — high-level operations.

v0.1 surface:
  - register_worker(worker_id, name, role, profile, authority) -> Worker
  - update_worker(worker_id, changes) -> bumps worker.version, emits worker.updated
  - retire_worker(worker_id)
  - register_node(host, capabilities) -> Node (emits node.joined)
  - heartbeat(node_id) -> updates last_heartbeat_at (emits node.heartbeat)
  - drain_node(node_id)
  - assign_worker_to_node(worker_id, node_id) -> emits worker.assigned

Per ADR 0003: workers and nodes are separate entities.

TODO(v0.1): implement service methods. Repository (Postgres) in
registry/repository.py.
"""
