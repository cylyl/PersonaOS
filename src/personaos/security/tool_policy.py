"""Tool allow/deny enforcement.

Per ADR 0004 & ADR 0020:
Evaluated before tool execution. Enforces worker capabilities and active policy rules.
Fail-closed: if not explicitly granted or allowed by policy, denies execution.
"""

from __future__ import annotations

import fnmatch
from typing import Any

from personaos.domain.errors import PermissionDeniedError
from personaos.security.policy import (
    Decision,
    Effect,
    Policy,
    PolicyEngine,
    get_default_policy_engine,
)


class ToolPolicy:
    """Tool execution policy enforcement."""

    def __init__(self, engine: PolicyEngine | None = None) -> None:
        self._engine = engine or get_default_policy_engine()

    def check(
        self,
        worker: Any,
        tool_name: str,
        tool_args: dict[str, Any] | None = None,
        resource: str = "*",
        context: dict[str, Any] | None = None,
        policies: list[Policy] | None = None,
    ) -> Decision:
        """Check whether worker is authorized to execute tool with given args/resource.

        Returns Decision (ALLOW, DENY, REQUIRE_APPROVAL).
        """
        if worker is None:
            return Decision(
                effect=Effect.DENY,
                reason="Missing worker identity for tool execution",
                matched=False,
            )

        # 1. Capability check (if worker has capabilities list defined and non-empty)
        capabilities: list[str] = []
        if hasattr(worker, "capabilities"):
            capabilities = list(worker.capabilities or [])
        elif isinstance(worker, dict):
            capabilities = list(worker.get("capabilities") or [])

        if capabilities:
            allowed = False
            for cap in capabilities:
                if cap in ("*", "tool:*", tool_name, f"tool:{tool_name}"):
                    allowed = True
                    break
                match_plain = fnmatch.fnmatchcase(tool_name, cap)
                match_prefixed = fnmatch.fnmatchcase(f"tool:{tool_name}", cap)
                if match_plain or match_prefixed:
                    allowed = True
                    break
            if not allowed:
                return Decision(
                    effect=Effect.DENY,
                    reason=f"Tool {tool_name!r} not granted in worker capabilities",
                    matched=False,
                )

        # 2. Evaluate against PolicyEngine
        ctx = dict(context or {})
        ctx["tool_name"] = tool_name
        ctx["tool_args"] = tool_args or {}

        action = f"tool:{tool_name}" if not tool_name.startswith("tool:") else tool_name

        return self._engine.evaluate(
            actor=worker,
            action=action,
            resource=resource,
            context=ctx,
            policies=policies,
        )

    def enforce(
        self,
        worker: Any,
        tool_name: str,
        tool_args: dict[str, Any] | None = None,
        resource: str = "*",
        context: dict[str, Any] | None = None,
        policies: list[Policy] | None = None,
    ) -> Decision:
        """Enforce tool authorization.

        Raises PermissionDeniedError if effect is DENY.
        Returns Decision otherwise (ALLOW or REQUIRE_APPROVAL).
        """
        decision = self.check(
            worker=worker,
            tool_name=tool_name,
            tool_args=tool_args,
            resource=resource,
            context=context,
            policies=policies,
        )
        if decision.is_denied:
            raise PermissionDeniedError(decision.reason)
        return decision


_default_tool_policy = ToolPolicy()


def check(
    worker: Any,
    tool_name: str,
    tool_args: dict[str, Any] | None = None,
    resource: str = "*",
    context: dict[str, Any] | None = None,
    policies: list[Policy] | None = None,
) -> Decision:
    """Convenience functional check using default ToolPolicy."""
    return _default_tool_policy.check(
        worker=worker,
        tool_name=tool_name,
        tool_args=tool_args,
        resource=resource,
        context=context,
        policies=policies,
    )
