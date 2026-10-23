"""Unit tests for security policy engine and schemas/policy.yaml adherence."""

from __future__ import annotations

from personaos.security.policy import Effect, Policy, PolicyEngine, Rule


def test_fail_closed_when_no_policies():
    """Fail-closed: returns DENY when no policies are registered."""
    engine = PolicyEngine(policies=[])
    decision = engine.evaluate(actor="worker-1", action="tool:bash", resource="prod-server")
    assert decision.is_denied
    assert decision.effect == Effect.DENY
    assert not decision.matched


def test_fail_closed_when_no_applicable_policies():
    """Fail-closed: returns DENY when policies exist but do not apply to target role/actor."""
    policy = Policy(
        id="devops-only",
        name="DevOps Policy",
        priority=10,
        default_effect=Effect.ALLOW,
        applies_to={"worker_roles": ["devops"]},
        rules=[Rule(id="allow-all", action="*", resource="*", effect=Effect.ALLOW)],
    )
    engine = PolicyEngine([policy])

    # Worker with 'qa' role does not match applies_to
    qa_actor = {"id": "worker-qa-1", "role": "qa"}
    decision = engine.evaluate(actor=qa_actor, action="tool:bash", resource="test")
    assert decision.is_denied
    assert "No applicable security policies" in decision.reason


def test_exact_rule_matching():
    """Exact string match on action, resource, and worker_id."""
    rule = Rule(
        id="exact-rule",
        action="tool:deploy",
        resource="staging-cluster",
        worker_id="worker-devops-01",
        effect=Effect.ALLOW,
    )
    policy = Policy(
        id="exact-policy",
        name="Exact Policy",
        priority=1,
        rules=[rule],
        default_effect=Effect.DENY,
    )
    engine = PolicyEngine([policy])

    # Matching actor
    d1 = engine.evaluate(
        actor={"id": "worker-devops-01"}, action="tool:deploy", resource="staging-cluster"
    )
    assert d1.is_allowed
    assert d1.rule_id == "exact-rule"

    # Non-matching actor
    d2 = engine.evaluate(
        actor={"id": "worker-devops-02"}, action="tool:deploy", resource="staging-cluster"
    )
    assert d2.is_denied

    # Non-matching action
    d3 = engine.evaluate(
        actor={"id": "worker-devops-01"}, action="tool:destroy", resource="staging-cluster"
    )
    assert d3.is_denied


def test_glob_matching():
    """Glob syntax (* and ?) matching for action, resource, and worker."""
    rule = Rule(
        id="glob-rule",
        action="tool:*",
        resource="staging-*",
        worker_id="worker-devops-*",
        effect=Effect.ALLOW,
    )
    policy = Policy(id="glob-pol", name="Glob", rules=[rule], default_effect=Effect.DENY)
    engine = PolicyEngine([policy])

    d = engine.evaluate(
        actor={"id": "worker-devops-999"},
        action="tool:kubectl",
        resource="staging-us-east-1",
    )
    assert d.is_allowed
    assert d.rule_id == "glob-rule"

    # Mismatched resource (prod instead of staging)
    d_prod = engine.evaluate(
        actor={"id": "worker-devops-999"},
        action="tool:kubectl",
        resource="prod-us-east-1",
    )
    assert d_prod.is_denied


def test_priority_overrides():
    """Higher priority policy overrides lower priority policy."""
    lower_policy = Policy(
        id="default-allow",
        name="Low Priority Allow",
        priority=10,
        rules=[Rule(id="allow-all", action="*", resource="*", effect=Effect.ALLOW)],
    )
    higher_policy = Policy(
        id="strict-deny",
        name="High Priority Deny",
        priority=100,
        rules=[
            Rule(
                id="deny-destructive",
                action="tool:rm",
                resource="prod-*",
                effect=Effect.DENY,
            )
        ],
        default_effect=Effect.ALLOW,
    )

    engine = PolicyEngine([lower_policy, higher_policy])

    # For tool:rm on prod-*, higher policy rule matches first
    decision = engine.evaluate(actor="any-worker", action="tool:rm", resource="prod-db")
    assert decision.is_denied
    assert decision.policy_id == "strict-deny"
    assert decision.rule_id == "deny-destructive"

    # For tool:read, strict-deny has no matching rule, but lower policy has allow-all rule
    decision2 = engine.evaluate(actor="any-worker", action="tool:read", resource="prod-db")
    assert decision2.is_allowed
    assert decision2.policy_id == "default-allow"


def test_first_match_wins_within_policy():
    """Rules evaluated top to bottom; first match wins."""
    policy = Policy(
        id="ordered-rules",
        name="Ordered Rules",
        priority=50,
        rules=[
            Rule(id="deny-rule", action="tool:reboot", resource="*", effect=Effect.DENY),
            Rule(id="allow-rule", action="tool:*", resource="*", effect=Effect.ALLOW),
        ],
        default_effect=Effect.DENY,
    )
    engine = PolicyEngine([policy])

    d_reboot = engine.evaluate(actor="worker", action="tool:reboot", resource="node-1")
    assert d_reboot.is_denied
    assert d_reboot.rule_id == "deny-rule"

    d_other = engine.evaluate(actor="worker", action="tool:status", resource="node-1")
    assert d_other.is_allowed
    assert d_other.rule_id == "allow-rule"


def test_require_approval_effect():
    """Policy can mandate human approval."""
    policy = Policy(
        id="approval-policy",
        name="Requires Approval",
        priority=10,
        rules=[
            Rule(
                id="gate-prod-deploy",
                action="task:deploy",
                resource="production",
                effect=Effect.REQUIRE_APPROVAL,
            )
        ],
        default_effect=Effect.DENY,
    )
    engine = PolicyEngine([policy])

    d = engine.evaluate(actor="worker-1", action="task:deploy", resource="production")
    assert d.requires_approval
    assert d.effect == Effect.REQUIRE_APPROVAL
    assert d.rule_id == "gate-prod-deploy"


def test_condition_source_ip_cidr():
    """Rule conditions evaluate source IP CIDR blocks."""
    rule = Rule(
        id="internal-subnet-only",
        action="tool:ssh",
        resource="*",
        effect=Effect.ALLOW,
        conditions={"source_ip": ["10.0.0.0/8", "192.168.1.0/24"]},
    )
    policy = Policy(id="ip-policy", name="IP", rules=[rule], default_effect=Effect.DENY)
    engine = PolicyEngine([policy])

    # Internal IP matches
    d1 = engine.evaluate(
        actor="w1",
        action="tool:ssh",
        resource="bastion",
        context={"client_ip": "10.1.2.3"},
    )
    assert d1.is_allowed

    # External IP rejected
    d2 = engine.evaluate(
        actor="w1",
        action="tool:ssh",
        resource="bastion",
        context={"client_ip": "203.0.113.195"},
    )
    assert d2.is_denied

    # Missing IP rejected
    d3 = engine.evaluate(actor="w1", action="tool:ssh", resource="bastion", context={})
    assert d3.is_denied


def test_policy_yaml_deserialization():
    """Load policy from YAML conforming to schemas/policy.yaml."""
    yaml_text = """
id: qa-default
name: QA Default Policy
description: Default policy for QA workers
priority: 20
default_effect: deny
applies_to:
  worker_roles:
    - qa
rules:
  - id: allow-test-tools
    action: "tool:test:*"
    resource: "staging-*"
    effect: allow
  - id: require-approval-staging-db
    action: "tool:db:*"
    resource: "staging-db"
    effect: require_approval
"""
    policy = Policy.from_yaml(yaml_text)
    assert policy.id == "qa-default"
    assert policy.priority == 20
    assert policy.default_effect == Effect.DENY
    assert len(policy.rules) == 2
    assert policy.rules[0].effect == Effect.ALLOW
    assert policy.rules[1].effect == Effect.REQUIRE_APPROVAL
