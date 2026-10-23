"""Unit tests for secrets resolution and redaction."""

from __future__ import annotations

import pytest

from personaos.domain.errors import SecretNotFoundError
from personaos.security.quarantine import is_quarantined
from personaos.security.secrets import SecretsManager


def test_secrets_resolution_from_static_and_env(monkeypatch: pytest.MonkeyPatch):
    """Resolve secret from static registry and environment variable."""
    mgr = SecretsManager({"STATIC_KEY": "static-secret-value-123"})
    monkeypatch.setenv("ENV_KEY", "env-secret-value-456")

    assert mgr.resolve("secret:STATIC_KEY") == "static-secret-value-123"
    assert mgr.resolve("secret:ENV_KEY") == "env-secret-value-456"

    with pytest.raises(SecretNotFoundError, match="not found"):
        mgr.resolve("secret:UNKNOWN_KEY")

    with pytest.raises(SecretNotFoundError, match="Invalid secret reference format"):
        mgr.resolve("invalid_ref_without_prefix")


def test_secrets_recursive_resolution():
    """Recursively resolve secrets inside nested dicts and lists."""
    mgr = SecretsManager({"DB_PASS": "pass123", "API_KEY": "key999"})
    payload = {
        "db": {"password": "secret:DB_PASS", "port": 5432},
        "headers": ["Authorization: secret:API_KEY", "Content-Type: application/json"],
    }
    resolved = mgr.resolve_references(payload)
    assert resolved["db"]["password"] == "pass123"
    assert resolved["db"]["port"] == 5432
    # only exact secret references match, not partial strings inside lists
    assert resolved["headers"][1] == "Content-Type: application/json"


def test_secrets_redaction():
    """Redact resolved secrets and bearer patterns from text and nested structures."""
    mgr = SecretsManager({"TOP_SECRET": "ultra-sensitive-data-string"})
    # Trigger resolution so manager knows the secret
    secret_val = mgr.resolve("secret:TOP_SECRET")
    assert secret_val == "ultra-sensitive-data-string"

    log_entry = {
        "message": "Connection established with secret ultra-sensitive-data-string",
        "auth": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.dummytoken",
    }
    redacted = mgr.redact(log_entry)
    assert "ultra-sensitive-data-string" not in redacted["message"]
    assert "[REDACTED_SECRET]" in redacted["message"]
    assert "Bearer [REDACTED_TOKEN]" in redacted["auth"]


def test_is_quarantined_helper():
    """is_quarantined correctly identifies worker status."""

    class DummyWorker:
        def __init__(self, status: str):
            self.status = status

    assert is_quarantined(DummyWorker("quarantined"))
    assert not is_quarantined(DummyWorker("idle"))
    assert not is_quarantined(DummyWorker("busy"))
    assert is_quarantined({"status": "quarantined"})
    assert not is_quarantined({"status": "idle"})
