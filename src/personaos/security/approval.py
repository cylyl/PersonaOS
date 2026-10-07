"""Human approval gate.

v0.1 flow:
  1. Policy evaluates to REQUIRE_APPROVAL
  2. Runner calls approval.request(task, approvers) -> emits approval.requested
  3. Task status moves to 'blocked'
  4. Human POSTs /tasks/{id}/approve (or /reject) via API
  5. approval.approve(task_id, approver) -> task returns to 'queued' for re-claim

If no approver acts before APPROVAL_TIMEOUT_SECONDS, the task fails.

TODO(v0.1): implement request(task, approvers), approve(task_id, approver),
reject(task_id, approver, reason). Backed by a pending_approvals table.
"""
