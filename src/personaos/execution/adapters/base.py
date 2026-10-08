"""Adapter Protocol — the runtime contract.

Per ADR 0005: zero PersonaOS domain logic lives in adapters. The runner
calls adapter.execute(context, checkpoint) -> Result and nothing else.

Per ADR 0009: the adapter receives a frozen WorkerExecutionContext
(worker + snapshot profile + task), not a bare task. The snapshot
contract is in the type system — an adapter cannot accidentally read
worker.current_profile_version because that field is not on the context.

Concrete adapters (openclaw.py now; local.py, cloud.py later) implement
this Protocol.

TODO(v0.2): define the Result dataclass:
  - status: Literal["completed", "failed", "needs_input"]
  - output: dict | None
  - error: str | None
  - new_checkpoint: dict | None  # for resumption
"""

from __future__ import annotations

from typing import Any, Protocol

from personaos.execution.context import WorkerExecutionContext


class Adapter(Protocol):
    """Runtime adapter contract. Receives a frozen execution context."""

    async def execute(
        self,
        context: WorkerExecutionContext,
        checkpoint: Any | None,
    ) -> Any: ...
