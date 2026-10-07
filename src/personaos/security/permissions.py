"""RBAC / authority — worker permission resolution.

v0.1 scope: resolve a worker's effective permissions from:
  - worker.authority.tools (allow-list)
  - worker.authority.resource_scopes
  - worker.authority.requires_approval_for (always require human sign-off)
  - the active Policy (priority order — see security/policy.py)

TODO(v0.1): implement resolve(worker, action, resource, context) -> PermissionSet.
The PermissionSet tells the caller: which tools are allowed, which resources
are scoped, which actions always need approval.
"""
