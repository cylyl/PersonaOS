"""Domain-level errors.

Raised by services in registry/, workload/, security/, execution/.
Caught at the API boundary (api/deps.py, api/routes/*) and translated to
HTTP responses.

TODO(v0.1): define WorkerNotFound, TaskNotFound, NodeLost, Quarantined,
PolicyDenied, ApprovalRequired, LeaseExpired.
"""
