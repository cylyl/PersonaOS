"""Policy entity + evaluation engine.

Per ADR 0004 & ADR 0020:
Security surrounds every action in the runner main loop.
Fail-closed default-deny authorization engine adhering strictly to schemas/policy.yaml.
"""

from __future__ import annotations

import fnmatch
import ipaddress
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml


class Effect(StrEnum):
    """Authorization decision effect."""

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True)
class Rule:
    """Single authorization rule within a policy."""

    effect: Effect
    id: str | None = None
    description: str | None = None
    action: str = "*"
    resource: str = "*"
    worker_id: str | None = None
    conditions: dict[str, Any] = field(default_factory=dict)

    def matches(
        self,
        *,
        actor_id: str | None,
        action: str,
        resource: str,
        context: dict[str, Any] | None = None,
    ) -> bool:
        """Check whether this rule matches the requested action, resource, actor, and conditions."""
        if not fnmatch.fnmatchcase(action, self.action):
            return False

        if not fnmatch.fnmatchcase(resource, self.resource):
            return False

        if self.worker_id is not None:
            if actor_id is None:
                return False
            if not fnmatch.fnmatchcase(actor_id, self.worker_id):
                return False

        if self.conditions:
            ctx = context or {}
            # CIDR source_ip check
            source_ips = self.conditions.get("source_ip")
            if source_ips:
                client_ip_str = ctx.get("source_ip") or ctx.get("client_ip")
                if not client_ip_str:
                    return False
                try:
                    client_ip = ipaddress.ip_address(client_ip_str)
                    matched_ip = False
                    for cidr in source_ips:
                        net = ipaddress.ip_network(cidr, strict=False)
                        if client_ip in net:
                            matched_ip = True
                            break
                    if not matched_ip:
                        return False
                except ValueError:
                    return False

        return True


@dataclass(frozen=True)
class Policy:
    """Security authorization policy loaded per schemas/policy.yaml."""

    id: str
    name: str
    priority: int = 0
    default_effect: Effect = Effect.DENY
    description: str | None = None
    rules: list[Rule] = field(default_factory=list)
    applies_to: dict[str, Any] = field(default_factory=dict)
    version: int = 1

    def matches_target(
        self,
        *,
        actor_id: str | None,
        actor_role: str | None,
        task_type: str | None,
    ) -> bool:
        """Check whether policy applies to the target actor and task type."""
        if not self.applies_to:
            return True

        worker_roles = self.applies_to.get("worker_roles")
        if worker_roles and (not actor_role or actor_role not in worker_roles):
            return False

        worker_ids = self.applies_to.get("worker_ids")
        if worker_ids and (not actor_id or actor_id not in worker_ids):
            return False

        task_types = self.applies_to.get("task_types")
        return not (task_types and (not task_type or task_type not in task_types))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Policy:
        """Construct Policy from dictionary conforming to schemas/policy.yaml."""
        raw_rules = data.get("rules", [])
        rules: list[Rule] = []
        for r in raw_rules:
            effect_val = r["effect"]
            effect_enum = Effect(effect_val.lower()) if isinstance(effect_val, str) else effect_val
            rules.append(
                Rule(
                    id=r.get("id"),
                    description=r.get("description"),
                    action=r.get("action", "*"),
                    resource=r.get("resource", "*"),
                    worker_id=r.get("worker_id"),
                    effect=effect_enum,
                    conditions=r.get("conditions", {}),
                )
            )

        raw_default = data.get("default_effect", "deny")
        default_enum = Effect(raw_default.lower()) if isinstance(raw_default, str) else raw_default

        return cls(
            id=data["id"],
            name=data["name"],
            priority=int(data.get("priority", 0)),
            default_effect=default_enum,
            description=data.get("description"),
            rules=rules,
            applies_to=data.get("applies_to", {}),
            version=int(data.get("version", 1)),
        )

    @classmethod
    def from_yaml(cls, path_or_content: str | Path) -> Policy:
        """Load Policy from YAML file path or YAML content string."""
        is_path_str = (
            isinstance(path_or_content, str)
            and "\n" not in path_or_content
            and Path(path_or_content).exists()
        )
        if isinstance(path_or_content, Path) or is_path_str:
            p = Path(path_or_content)
            raw = yaml.safe_load(p.read_text(encoding="utf-8"))
        else:
            raw = yaml.safe_load(str(path_or_content))
        return cls.from_dict(raw)


@dataclass(frozen=True)
class Decision:
    """Result of policy evaluation."""

    effect: Effect
    policy_id: str | None = None
    rule_id: str | None = None
    reason: str = ""
    matched: bool = False

    @property
    def is_allowed(self) -> bool:
        """True if effect is ALLOW."""
        return self.effect == Effect.ALLOW

    @property
    def is_denied(self) -> bool:
        """True if effect is DENY."""
        return self.effect == Effect.DENY

    @property
    def requires_approval(self) -> bool:
        """True if effect is REQUIRE_APPROVAL."""
        return self.effect == Effect.REQUIRE_APPROVAL


class PolicyEngine:
    """Thread-safe, stateless policy evaluation engine."""

    def __init__(self, policies: list[Policy] | None = None) -> None:
        self._policies: list[Policy] = list(policies or [])

    @property
    def policies(self) -> list[Policy]:
        """Registered policies."""
        return list(self._policies)

    def register_policy(self, policy: Policy) -> None:
        """Register a policy."""
        # Replace if same ID
        self._policies = [p for p in self._policies if p.id != policy.id] + [policy]

    def load_policies_from_dir(self, dir_path: Path) -> list[Policy]:
        """Load all *.yaml policy files from directory."""
        loaded: list[Policy] = []
        if not dir_path.exists() or not dir_path.is_dir():
            return loaded

        for file_path in sorted(dir_path.glob("*.yaml")):
            try:
                pol = Policy.from_yaml(file_path)
                self.register_policy(pol)
                loaded.append(pol)
            except Exception:
                continue
        return loaded

    def evaluate(
        self,
        actor: Any,
        action: str,
        resource: str = "*",
        context: dict[str, Any] | None = None,
        policies: list[Policy] | None = None,
    ) -> Decision:
        """Evaluate action and resource against applicable policies.

        First match wins within policy rules ordered by policy priority DESC.
        Fail-closed by default.
        """
        candidate_policies = policies if policies is not None else self._policies
        if not candidate_policies:
            return Decision(
                effect=Effect.DENY,
                reason="No security policies configured (fail-closed default)",
                matched=False,
            )

        actor_id: str | None = None
        actor_role: str | None = None
        if actor is not None:
            if hasattr(actor, "id"):
                actor_id = actor.id
            elif isinstance(actor, dict):
                actor_id = actor.get("id")
            elif isinstance(actor, str):
                actor_id = actor

            if hasattr(actor, "role"):
                actor_role = actor.role
            elif isinstance(actor, dict):
                actor_role = actor.get("role")

        ctx = context or {}
        task = ctx.get("task")
        task_type = ctx.get("task_type")
        if not task_type and task is not None:
            task_type = getattr(task, "type", None)

        # 1. Filter applicable policies
        applicable: list[Policy] = [
            p
            for p in candidate_policies
            if p.matches_target(
                actor_id=actor_id,
                actor_role=actor_role,
                task_type=task_type,
            )
        ]

        if not applicable:
            return Decision(
                effect=Effect.DENY,
                reason="No applicable security policies for actor/task (fail-closed default)",
                matched=False,
            )

        # 2. Sort by priority DESC, then id ASC
        applicable.sort(key=lambda p: (-p.priority, p.id))

        # 3. Walk rules across policies in priority order; first match wins
        for policy in applicable:
            for rule in policy.rules:
                if rule.matches(
                    actor_id=actor_id,
                    action=action,
                    resource=resource,
                    context=ctx,
                ):
                    return Decision(
                        effect=rule.effect,
                        policy_id=policy.id,
                        rule_id=rule.id,
                        reason=f"Matched rule {rule.id or 'unnamed'} in policy {policy.id}",
                        matched=True,
                    )

        # 4. No matching rule -> apply default_effect of highest priority policy
        highest = applicable[0]
        return Decision(
            effect=highest.default_effect,
            policy_id=highest.id,
            reason=(
                f"No matching rule; applied default_effect '{highest.default_effect.value}' "
                f"of policy {highest.id}"
            ),
            matched=False,
        )


# Global default policy engine instance
_default_engine = PolicyEngine()


def get_default_policy_engine() -> PolicyEngine:
    """Return singleton PolicyEngine, auto-loading from policies/ if available."""
    global _default_engine
    if not _default_engine.policies:
        from personaos.config import settings

        dir_to_load: Path | None = None
        if settings.policies_dir:
            dir_to_load = Path(settings.policies_dir)
        else:
            default_dir = Path(__file__).resolve().parents[3] / "policies"
            if default_dir.exists() and default_dir.is_dir():
                dir_to_load = default_dir

        if dir_to_load is not None:
            _default_engine.load_policies_from_dir(dir_to_load)

    return _default_engine


def evaluate(
    actor: Any,
    action: str,
    resource: str = "*",
    context: dict[str, Any] | None = None,
    policies: list[Policy] | None = None,
) -> Decision:
    """Convenience functional evaluation using default engine."""
    return _default_engine.evaluate(
        actor=actor,
        action=action,
        resource=resource,
        context=context,
        policies=policies,
    )
