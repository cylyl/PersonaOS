"""Task entity — durable unit of work.

Eight lifecycle states: queued, assigned, in_progress, blocked, review,
completed, failed, canceled. State machine enforced by execution/runner.py
and workload/service.py.

TODO(v0.1): define Task dataclass + Lifecycle enum.
Source of truth: schemas/task.yaml.
"""
