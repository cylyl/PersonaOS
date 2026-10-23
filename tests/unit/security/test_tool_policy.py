"""Unit tests for tool policy and permissions."""

from __future__ import annotations

import pytest

from personaos.domain.errors import PermissionDeniedError
from personaos.security.permissions import resolve
from personaos.security.policy import Effect, Policy, PolicyEngine, Rule
from personaos.security.tool_policy import ToolPolicy


def test_tool_policy_denies_missing_worker():
    """Tool execution denied when worker identity is absent."""
    tp = ToolPolicy()
    decision = tp.check(worker=None, tool_name="bash")
    assert decision.is_denied
    assert "Missing worker identity" in decision.reason


def test_tool_policy_enforces_worker_capabilities():
    """Worker without tool in capabilities is rejected before policy evaluation."""
    worker = {"id": "w1", "capabilities": ["kubectl", "git"]}
    tp = ToolPolicy()
    decision = tp.check(worker=worker, tool_name="rm")
    assert decision.is_denied
    assert "not granted in worker capabilities" in decision.reason

    # Enforce raises PermissionDeniedError
    with pytest.raises(PermissionDeniedError, match="not granted in worker capabilities"):
        tp.enforce(worker=worker, tool_name="rm")


def test_tool_policy_allows_matching_capability_and_policy():
    """Worker with tool in capabilities and matching policy is allowed."""
    policy = Policy(
        id="dev-tools",
        name="Dev Tools",
        rules=[Rule(id="allow-git", action="tool:git", resource="*", effect=Effect.ALLOW)],
        default_effect=Effect.DENY,
    )
    engine = PolicyEngine([policy])
    tp = ToolPolicy(engine=engine)

    worker = {"id": "w1", "capabilities": ["git"]}
    decision = tp.check(worker=worker, tool_name="git")
    assert decision.is_allowed
    assert decision.rule_id == "allow-git"

    # Enforce succeeds without exception
    d_enforced = tp.enforce(worker=worker, tool_name="git")
    assert d_enforced.is_allowed


def test_tool_policy_requires_approval():
    """Tool invocation returning REQUIRE_APPROVAL returns the appropriate decision."""
    policy = Policy(
        id="deploy-policy",
        name="Deploy",
        rules=[
            Rule(
                id="gate-prod-deploy",
                action="tool:deploy",
                resource="prod",
                effect=Effect.REQUIRE_APPROVAL,
            )
        ],
        default_effect=Effect.DENY,
    )
    engine = PolicyEngine([policy])
    tp = ToolPolicy(engine=engine)

    worker = {"id": "w1", "capabilities": ["deploy"]}
    decision = tp.check(worker=worker, tool_name="deploy", resource="prod")
    assert decision.requires_approval
    assert decision.rule_id == "gate-prod-deploy"


def test_permissions_resolution():
    """PermissionSet resolves effective tools and resource scopes."""
    policy = Policy(
        id="test-pol",
        name="Test",
        rules=[
            Rule(id="allow-read", action="tool:cat", resource="*", effect=Effect.ALLOW),
            Rule(
                id="gate-write",
                action="tool:write",
                resource="*",
                effect=Effect.REQUIRE_APPROVAL,
            ),
        ],
        default_effect=Effect.DENY,
    )
    engine = PolicyEngine([policy])

    worker = {
        "id": "w1",
        "capabilities": ["cat", "write", "delete"],
        "permissions": ["staging.read"],
    }
    perm_set = resolve(worker, engine=engine)
    assert perm_set.is_tool_allowed("cat")
    assert not perm_set.is_tool_allowed("delete")  # Denied by default_effect
    assert perm_set.is_resource_allowed("staging.read")
    assert "write" in perm_set.requires_approval_for
