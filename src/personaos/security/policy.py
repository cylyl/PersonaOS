"""Policy entity + evaluation engine.

v0.1 scope: entity and engine in one module. Acceptable for v0.1; flagged as
tech debt — split into domain/policy.py + security/evaluator.py in v0.2 if
the entity needs to be unit-tested in isolation.

Decision flow:
  1. Resolve applicable policies for (worker, task) — order by priority DESC
  2. For each policy, walk rules in order; first match wins
  3. Match = action glob AND resource glob AND (worker_id glob or None) AND conditions
  4. Effect -> ALLOW | DENY | REQUIRE_APPROVAL
  5. No match in any policy -> policy.default_effect (default: deny, fail-closed)

TODO(v0.1): define Policy, Rule, Effect, Decision dataclasses. Implement
evaluate(actor, action, resource, context) -> Decision.

Source of truth: schemas/policy.yaml. Glob matching via fnmatch.
"""
