"""Task routes — enqueue, approve, list.

TODO(v0.1):
  POST   /tasks                        # enqueue (returns task id + status)
  GET    /tasks/{id}
  GET    /tasks?status=...&worker=...
  POST   /tasks/{id}/approve           # human approves a require_approval task
  POST   /tasks/{id}/reject            # human rejects
"""
