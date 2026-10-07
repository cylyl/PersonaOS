"""Scheduler — FIFO + priority + skill-match in SQL.

Per the kernel architecture: deliberately simple for v0.1. No AI scheduler.

The atomic claim pattern:

    SELECT ...
    FROM tasks
    WHERE status = 'queued'
      AND (required_skills <@ worker_skills OR required_skills = '[]')
      AND (deadline IS NULL OR deadline > NOW())
    ORDER BY priority DESC, created_at ASC
    FOR UPDATE SKIP LOCKED
    LIMIT 1;

    UPDATE tasks SET status='assigned', worker_id=?, node_id=?, claimed_at=NOW()
    WHERE id = ?;

TODO(v0.1): implement as a single async transaction. Worker skills come from
worker.authority.tools + worker profile.
"""
