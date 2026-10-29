"""Ephemeral TaskAccessToken issuance and verification.

Per ADR 0035 — Cross-Machine Agent Execution Protocol (PMAEP):
Cryptographically signs and verifies ephemeral task access tokens
to prevent cross-tenant forging, stale replay, and generation mismatches.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from personaos.protocol.errors import (
    AuthenticationFailedError,
    StaleGenerationProtocolError,
)


class TaskTokenManager:
    """Issues and verifies ephemeral HMAC-SHA256 task tokens."""

    def __init__(self, secret_key: str = "personaos-cluster-default-secret-change-in-prod") -> None:
        self.secret_key = secret_key.encode("utf-8")

    def issue_token(
        self,
        *,
        worker_id: str,
        node_id: str,
        task_id: str,
        generation: int,
        ttl_seconds: float = 3600.0,
        scopes: list[str] | None = None,
    ) -> str:
        """Issues a signed JWT-like token string."""
        now = time.time()
        payload = {
            "sub": worker_id,
            "nid": node_id,
            "tid": task_id,
            "gen": generation,
            "iat": int(now),
            "exp": int(now + ttl_seconds),
            "scopes": scopes or ["checkpoint:write", "telemetry:emit"],
        }
        header = {"alg": "HS256", "typ": "JWT"}

        hdr_b64 = base64.urlsafe_b64encode(
            json.dumps(header, separators=(",", ":")).encode()
        ).decode("utf-8").rstrip("=")
        payload_b64 = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode()
        ).decode("utf-8").rstrip("=")

        signing_input = f"{hdr_b64}.{payload_b64}".encode()
        sig = hmac.new(self.secret_key, signing_input, hashlib.sha256).digest()
        sig_b64 = base64.urlsafe_b64encode(sig).decode("utf-8").rstrip("=")

        return f"{hdr_b64}.{payload_b64}.{sig_b64}"

    def verify_token(
        self,
        token: str,
        *,
        expected_task_id: str | None = None,
        expected_node_id: str | None = None,
        expected_generation: int | None = None,
    ) -> dict[str, Any]:
        """Validates token signature, expiration, and identity claims."""
        parts = token.split(".")
        if len(parts) != 3:
            raise AuthenticationFailedError("Malformed token structure")

        hdr_b64, payload_b64, sig_b64 = parts
        signing_input = f"{hdr_b64}.{payload_b64}".encode()
        expected_sig = hmac.new(self.secret_key, signing_input, hashlib.sha256).digest()
        expected_sig_b64 = base64.urlsafe_b64encode(expected_sig).decode("utf-8").rstrip("=")

        if not hmac.compare_digest(sig_b64, expected_sig_b64):
            raise AuthenticationFailedError("Invalid cryptographic token signature")

        # Decode payload
        padded_payload = payload_b64 + "=" * (-len(payload_b64) % 4)
        try:
            payload_data = json.loads(base64.urlsafe_b64decode(padded_payload).decode("utf-8"))
        except Exception as e:
            raise AuthenticationFailedError(f"Corrupted token payload: {e}") from e

        if not isinstance(payload_data, dict):
            raise AuthenticationFailedError("Corrupted token payload: expected JSON object")
        payload: dict[str, Any] = payload_data

        # Expiration check
        if time.time() > payload.get("exp", 0):
            raise AuthenticationFailedError("Token has expired")

        # Claims verification
        if expected_task_id and payload.get("tid") != expected_task_id:
            raise AuthenticationFailedError(
                f"Token task mismatch: expected {expected_task_id}, got {payload.get('tid')}"
            )

        if expected_node_id and payload.get("nid") != expected_node_id:
            raise AuthenticationFailedError(
                f"Token node mismatch: expected {expected_node_id}, got {payload.get('nid')}"
            )

        if expected_generation is not None and payload.get("gen") != expected_generation:
            actual_gen = payload.get("gen")
            raise StaleGenerationProtocolError(
                f"Token generation mismatch: expected {expected_generation}, got {actual_gen}"
            )

        return payload
