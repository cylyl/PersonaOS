"""Secret references and isolation.

v0.1 scope: workers reference secrets by NAME (e.g., "secret:prod-db-readonly"),
never by value. The runner resolves the value at execution time via the secrets
backend (env vars in v0.1; Vault/SSM post-v0.1) and never persists it.

Hard rules:
  - The worker's prompt or memory never contains a secret value.
  - The events log payload never contains a secret value.
  - The adapter receives the resolved value at execution time but does not log it.

TODO(v0.1): implement resolve(secret_ref) -> str (env-var-backed in v0.1);
audit_log(secret_ref, task_id, actor) -> emits an event WITHOUT the value.
"""
