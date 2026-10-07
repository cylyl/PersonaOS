"""Task lease — TTL-based lock for safe task execution and resume.

v0.1 surface:
  - lease(task_id, worker_id, node_id, ttl=300) -> Lease (emits task.claimed)
  - renew(lease_id) -> extends TTL
  - release(lease_id)
  - expire_overdue() -> list of expired lease IDs (called by scheduler loop)

If a worker dies mid-task, the lease expires; the scheduler reclaims and
reassigns. No silent task loss. This is what makes "tasks cannot silently
disappear from the durable queue" (README §14) enforceable.

TODO(v0.1): implement the leases table + helpers. The expire_overdue() call
runs on a short interval (e.g., every 10s) inside the runner.
"""
