"""Adapter Protocol — the runtime contract.

Per ADR 0005: zero PersonaOS domain logic lives in adapters. The runner
calls adapter.execute(context, checkpoint) -> Result and nothing else.

Per ADR 0009: the adapter receives a frozen WorkerExecutionContext
(worker + snapshot profile + task), not a bare task. The snapshot
contract is in the type system — an adapter cannot accidentally read
worker.current_profile_version because that field is not on the context.

Per ADR 0010: the adapter returns a Result. The kernel handles success
vs failure persistence.

Concrete adapters (openclaw.py now; local.py, cloud.py later) implement
this Protocol.
"""

from __future__ import annotations

from typing import Any, Protocol

from personaos.execution.context import WorkerExecutionContext
from personaos.execution.result import Result


class Adapter(Protocol):
    """Runtime adapter contract. Receives a frozen execution context."""

    async def execute(
        self,
        context: WorkerExecutionContext,
        checkpoint: Any | None,
    ) -> Result: ...
