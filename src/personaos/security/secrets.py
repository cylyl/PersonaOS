"""Secret references and isolation.

Per ADR 0004 & ADR 0020:
Workers and tasks reference secrets strictly by reference name (e.g. "secret:PROD_DB_PASS"),
never by raw value. Secrets are resolved at runtime and strictly redacted from
task results, diagnostic dictionaries, and event payloads.
"""

from __future__ import annotations

import os
import re
from typing import Any

from personaos.domain.errors import SecretNotFoundError

_SECRET_REF_PATTERN = re.compile(r"^secret:([A-Za-z0-9_\-\.]+)$")
_BEARER_PATTERN = re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]{8,}", re.IGNORECASE)


class SecretsManager:
    """Manages secret reference resolution and log/payload redaction."""

    def __init__(self, static_secrets: dict[str, str] | None = None) -> None:
        self._static_secrets = dict(static_secrets or {})
        self._known_secrets: set[str] = set()

    def register_secret(self, name: str, value: str) -> None:
        """Register a static secret for testing or in-memory config."""
        self._static_secrets[name] = value
        if len(value) >= 4:
            self._known_secrets.add(value)

    def is_secret_reference(self, value: Any) -> bool:
        """Check if a string is a secret reference (starts with 'secret:')."""
        if not isinstance(value, str):
            return False
        return bool(_SECRET_REF_PATTERN.match(value))

    def resolve(self, secret_ref: str) -> str:
        """Resolve a secret reference from environment variables or static secrets.

        Example: resolve('secret:DATABASE_PASSWORD') -> 'super-secret-pw'
        """
        match = _SECRET_REF_PATTERN.match(secret_ref)
        if not match:
            raise SecretNotFoundError(f"Invalid secret reference format: {secret_ref!r}")

        secret_name = match.group(1)

        # 1. Check registered static secrets
        if secret_name in self._static_secrets:
            val = self._static_secrets[secret_name]
            if len(val) >= 4:
                self._known_secrets.add(val)
            return val

        # 2. Check environment variables
        if secret_name in os.environ:
            val = os.environ[secret_name]
            if len(val) >= 4:
                self._known_secrets.add(val)
            return val

        raise SecretNotFoundError(
            f"Secret {secret_ref!r} ({secret_name}) not found in environment or secrets store"
        )

    def resolve_references(self, obj: Any) -> Any:
        """Recursively resolve all secret references in dicts, lists, or strings."""
        if isinstance(obj, str):
            if self.is_secret_reference(obj):
                return self.resolve(obj)
            return obj
        elif isinstance(obj, dict):
            return {k: self.resolve_references(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self.resolve_references(item) for item in obj]
        elif isinstance(obj, tuple):
            return tuple(self.resolve_references(item) for item in obj)
        return obj

    def redact(self, obj: Any) -> Any:
        """Recursively redact known secrets and bearer token patterns from data structures."""
        if isinstance(obj, str):
            redacted = obj
            # Redact known raw secret values
            for secret_val in self._known_secrets:
                if secret_val in redacted:
                    redacted = redacted.replace(secret_val, "[REDACTED_SECRET]")
            # Redact standard bearer auth patterns
            redacted = _BEARER_PATTERN.sub(r"\1[REDACTED_TOKEN]", redacted)
            return redacted
        elif isinstance(obj, dict):
            return {k: self.redact(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self.redact(item) for item in obj]
        elif isinstance(obj, tuple):
            return tuple(self.redact(item) for item in obj)
        return obj


_default_secrets_manager = SecretsManager()


def get_default_secrets_manager() -> SecretsManager:
    """Return singleton SecretsManager."""
    return _default_secrets_manager


def resolve(secret_ref: str) -> str:
    """Convenience resolve function using default SecretsManager."""
    return _default_secrets_manager.resolve(secret_ref)


def redact(obj: Any) -> Any:
    """Convenience redact function using default SecretsManager."""
    return _default_secrets_manager.redact(obj)
