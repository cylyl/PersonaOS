"""RBAC / authority — worker permission resolution.

Per ADR 0004 & ADR 0020:
Resolves a worker's effective permissions from:
  - worker.capabilities (tools allow-list)
  - worker.permissions (resource scopes)
  - active policies in PolicyEngine
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any

from personaos.security.policy import (
    Effect,
    Policy,
    PolicyEngine,
    get_default_policy_engine,
)


@dataclass(frozen=True)
class PermissionSet:
    """Resolved effective permissions for a worker."""

    tools: tuple[str, ...] = ()
    resource_scopes: tuple[str, ...] = ()
    requires_approval_for: tuple[str, ...] = ()

    def is_tool_allowed(self, tool_name: str) -> bool:
        """Check if tool is in effective allowed tools."""
        if "*" in self.tools or "tool:*" in self.tools:
            return True
        for pattern in self.tools:
            if pattern == tool_name or fnmatch.fnmatchcase(tool_name, pattern):
                return True
            if pattern == f"tool:{tool_name}" or fnmatch.fnmatchcase(f"tool:{tool_name}", pattern):
                return True
        return False

    def is_resource_allowed(self, resource: str) -> bool:
        """Check if resource is within allowed resource scopes."""
        if "*" in self.resource_scopes:
            return True
        for scope in self.resource_scopes:
            if scope == resource or fnmatch.fnmatchcase(resource, scope):
                return True
        return False

    def needs_approval(self, action: str) -> bool:
        """Check if action explicitly requires approval."""
        for pattern in self.requires_approval_for:
            if pattern == action or fnmatch.fnmatchcase(action, pattern):
                return True
        return False


def resolve(
    worker: Any,
    action: str = "*",
    resource: str = "*",
    context: dict[str, Any] | None = None,
    policies: list[Policy] | None = None,
    engine: PolicyEngine | None = None,
) -> PermissionSet:
    """Resolve a worker's effective permissions.

    Merges declared capabilities/permissions from the worker/profile with
    policy engine evaluations.
    """
    eng = engine or get_default_policy_engine()

    raw_tools: list[str] = []
    raw_scopes: list[str] = []

    if hasattr(worker, "capabilities") and worker.capabilities:
        raw_tools.extend(worker.capabilities)
    if hasattr(worker, "permissions") and worker.permissions:
        raw_scopes.extend(worker.permissions)

    if isinstance(worker, dict):
        raw_tools.extend(worker.get("capabilities", []))
        raw_scopes.extend(worker.get("permissions", []))

    # Evaluate against active policy engine
    ctx = context or {}
    requires_approval: list[str] = []
    allowed_tools: list[str] = []

    for tool in raw_tools:
        decision = eng.evaluate(
            actor=worker,
            action=f"tool:{tool}" if not tool.startswith("tool:") else tool,
            resource=resource,
            context=ctx,
            policies=policies,
        )
        if decision.effect == Effect.ALLOW:
            allowed_tools.append(tool)
        elif decision.effect == Effect.REQUIRE_APPROVAL:
            requires_approval.append(tool)
        # DENY simply excludes the tool from allowed_tools

    # If no policies restrict tools and worker had declared tools, default to declared
    if not policies and not eng.policies and raw_tools:
        allowed_tools = list(raw_tools)

    return PermissionSet(
        tools=tuple(allowed_tools),
        resource_scopes=tuple(raw_scopes),
        requires_approval_for=tuple(requires_approval),
    )
