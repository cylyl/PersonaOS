"""JSON-RPC 2.0 schemas and trace context envelopes for PMAEP.

Per ADR 0035 and docs/specs/cross-machine-agent-execution-protocol.md.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from pydantic import BaseModel, Field

METHOD_AGENT_HANDSHAKE = "agent.handshake"
METHOD_AGENT_DISPATCH = "agent.dispatch"
METHOD_AGENT_HEARTBEAT = "agent.heartbeat"
METHOD_AGENT_CHECKPOINT = "agent.checkpoint"
METHOD_AGENT_CANCEL = "agent.cancel"
METHOD_AGENT_COMPLETE = "agent.complete"


class TraceMetadata(BaseModel):
    """W3C-aligned distributed tracing and correlation context."""

    trace_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    span_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    correlation_id: str = Field(default_factory=lambda: f"corr-{uuid.uuid4().hex[:8]}")
    timestamp: float = Field(default_factory=time.time)


class RPCError(BaseModel):
    """JSON-RPC 2.0 error object."""

    code: int
    message: str
    data: Any = None


class RPCRequest(BaseModel):
    """JSON-RPC 2.0 request envelope."""

    jsonrpc: str = "2.0"
    id: str | int | None = None
    method: str
    params: dict[str, Any] = Field(default_factory=dict)
    meta: TraceMetadata = Field(default_factory=TraceMetadata)


class RPCResponse(BaseModel):
    """JSON-RPC 2.0 response envelope."""

    jsonrpc: str = "2.0"
    id: str | int | None = None
    result: Any = None
    error: RPCError | None = None
    meta: TraceMetadata = Field(default_factory=TraceMetadata)

    @classmethod
    def success(
        cls,
        request_id: str | int | None,
        result: Any,
        meta: TraceMetadata | None = None,
    ) -> RPCResponse:
        return cls(
            id=request_id,
            result=result,
            meta=meta or TraceMetadata(),
        )

    @classmethod
    def fail(
        cls,
        request_id: str | int | None,
        code: int,
        message: str,
        data: Any = None,
        meta: TraceMetadata | None = None,
    ) -> RPCResponse:
        return cls(
            id=request_id,
            error=RPCError(code=code, message=message, data=data),
            meta=meta or TraceMetadata(),
        )
