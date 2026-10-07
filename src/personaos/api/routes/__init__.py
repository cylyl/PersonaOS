"""API routes — workers, nodes, tasks, health.

v0.1 surface:
  /health                       — DB connectivity
  /workers                      — CRUD
  /workers/{id}/quarantine      — flip worker.status
  /nodes                        — register, heartbeat
  /tasks                        — enqueue, claim, complete, fail
  /events                       — read event log
"""
