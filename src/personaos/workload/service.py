"""Workload service — high-level operations on the durable queue.

v0.1 surface:
  - enqueue(task_type, payload, priority=50, required_skills=[], required_tools=[]) -> Task
  - claim_next(worker_id, node_id) -> Task | None   # delegates to scheduler
  - complete(task_id, result)
  - fail(task_id, reason)            # retries if retry_count < max_retries; else -> failed
  - cancel(task_id, reason)
  - checkpoint(task_id, checkpoint_ref)

State machine in domain/task.py. Per-task events emitted via events/store.py.

TODO(v0.1): implement the service. All state changes write to the events log
in the same transaction (per ADR 0006).
"""
