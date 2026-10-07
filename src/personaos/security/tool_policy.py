"""Tool allow/deny enforcement.

v0.1 scope: called by execution/runner.py BEFORE adapter.execute() for any
tool-using task. Returns allow | deny | require_approval. Logs policy.allowed
or policy.denied event accordingly.

TODO(v0.1): implement check(worker, tool_name, tool_args) -> Decision.
Reuses security/policy.py.evaluate() but specialized for tool actions.
"""
