"""Unit tests for Cross-Machine Agent Execution Protocol (PMAEP) (v0.2-C).

Per ADR 0035 and docs/specs/cross-machine-agent-execution-protocol.md.
"""

from __future__ import annotations

import time

import pytest

from personaos.protocol.errors import (
    AuthenticationFailedError,
    CapacityExceededError,
    CheckpointCorruptedError,
    PolicyViolationError,
    StaleGenerationProtocolError,
)
from personaos.protocol.models import (
    METHOD_AGENT_CHECKPOINT,
    METHOD_AGENT_DISPATCH,
    METHOD_AGENT_HANDSHAKE,
    RPCError,
    RPCRequest,
    RPCResponse,
    TraceMetadata,
)
from personaos.protocol.tokens import TaskTokenManager


def test_rpc_request_response_serialization() -> None:
    """Verifies standard JSON-RPC 2.0 serialization with TraceMetadata."""
    req = RPCRequest(
        id="req-123",
        method=METHOD_AGENT_HANDSHAKE,
        params={"node_id": "node-alpha"},
    )
    dumped = req.model_dump()
    assert dumped["jsonrpc"] == "2.0"
    assert dumped["id"] == "req-123"
    assert dumped["method"] == "agent.handshake"
    assert "trace_id" in dumped["meta"]

    # Success response
    resp = RPCResponse.success(req.id, result={"status": "ready"})
    assert resp.error is None
    assert resp.result["status"] == "ready"

    # Error response
    err_resp = RPCResponse.fail(req.id, -32001, "Auth failed")
    assert err_resp.result is None
    assert err_resp.error is not None
    assert err_resp.error.code == -32001
    assert err_resp.error.message == "Auth failed"


def test_custom_protocol_error_codes() -> None:
    """Verifies PersonaOS reserved error codes (-32000 to -32099)."""
    assert AuthenticationFailedError.code == -32001
    assert PolicyViolationError.code == -32003
    assert StaleGenerationProtocolError.code == -32009
    assert CapacityExceededError.code == -32015
    assert CheckpointCorruptedError.code == -32020


def test_task_token_manager_happy_path() -> None:
    """Verifies issuing and verifying a valid TaskAccessToken."""
    mgr = TaskTokenManager(secret_key="unit-test-secret-key")
    token = mgr.issue_token(
        worker_id="worker-devops-01",
        node_id="node-gpu-10",
        task_id="task-deploy-99",
        generation=3,
        ttl_seconds=60.0,
    )

    claims = mgr.verify_token(
        token,
        expected_task_id="task-deploy-99",
        expected_node_id="node-gpu-10",
        expected_generation=3,
    )
    assert claims["sub"] == "worker-devops-01"
    assert claims["nid"] == "node-gpu-10"
    assert claims["tid"] == "task-deploy-99"
    assert claims["gen"] == 3
    assert "checkpoint:write" in claims["scopes"]


def test_task_token_manager_signature_tampering() -> None:
    """Verifies that tampering with token payload is rejected."""
    mgr = TaskTokenManager(secret_key="unit-test-secret-key")
    token = mgr.issue_token(
        worker_id="worker-devops-01",
        node_id="node-gpu-10",
        task_id="task-deploy-99",
        generation=3,
    )
    parts = token.split(".")
    # Tamper with payload
    tampered_token = f"{parts[0]}.eyJob2dnZWQiOiB0cnVlfQ.{parts[2]}"
    with pytest.raises(AuthenticationFailedError, match="Invalid cryptographic token signature"):
        mgr.verify_token(tampered_token)


def test_task_token_manager_expiration() -> None:
    """Verifies expired tokens are rejected."""
    mgr = TaskTokenManager(secret_key="unit-test-secret-key")
    # Issue already expired token
    token = mgr.issue_token(
        worker_id="worker-1",
        node_id="node-1",
        task_id="task-1",
        generation=1,
        ttl_seconds=-10.0,
    )
    with pytest.raises(AuthenticationFailedError, match="Token has expired"):
        mgr.verify_token(token)


def test_task_token_manager_generation_mismatch() -> None:
    """Verifies generation mismatch raises StaleGenerationProtocolError."""
    mgr = TaskTokenManager(secret_key="unit-test-secret-key")
    token = mgr.issue_token(
        worker_id="worker-1",
        node_id="node-1",
        task_id="task-1",
        generation=2,
    )
    with pytest.raises(StaleGenerationProtocolError, match="Token generation mismatch"):
        mgr.verify_token(token, expected_generation=3)
