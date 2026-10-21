"""Simulation adapter — configurable, deterministic runtime adapter.

Per ADR 0005: only translates between runtime behavior and PersonaOS domain types.
Per ADR 0010: returns a Result; receives a frozen WorkerExecutionContext.
Per ADR 0011: supports static results, script handlers, sequential multi-step results,
error triggering, and execution tracing.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from personaos.execution.context import WorkerExecutionContext
from personaos.execution.result import Result


if TYPE_CHECKING:
    from personaos.execution.adapters.base import Adapter

HandlerType = Callable[[WorkerExecutionContext, Any | None], Result | Awaitable[Result]]


class SimulationAdapter:
    """Configurable adapter for simulations, testing, and task replays."""

    def __init__(
        self,
        *,
        result: Result | None = None,
        handler: HandlerType | None = None,
        sequence: list[Result] | None = None,
        error_trigger: Exception | None = None,
    ) -> None:
        self.result = result
        self.handler = handler
        self.sequence = list(sequence) if sequence is not None else None
        self.error_trigger = error_trigger

        self.captured_contexts: list[WorkerExecutionContext] = []
        self.captured_checkpoints: list[Any | None] = []
        self.call_count: int = 0

    def reset(self) -> None:
        """Reset captured call telemetry."""
        self.captured_contexts.clear()
        self.captured_checkpoints.clear()
        self.call_count = 0

    async def execute(
        self,
        context: WorkerExecutionContext,
        checkpoint: Any | None = None,
    ) -> Result:
        """Execute simulation step."""
        self.call_count += 1
        self.captured_contexts.append(context)
        self.captured_checkpoints.append(checkpoint)

        # 1. Error trigger takes precedence if set
        if self.error_trigger is not None:
            raise self.error_trigger

        # 2. Dynamic handler callback
        if self.handler is not None:
            res = self.handler(context, checkpoint)
            if inspect.isawaitable(res):
                return await res
            return res

        # 3. Sequential steps
        if self.sequence is not None:
            if not self.sequence:
                raise RuntimeError("SimulationAdapter sequence exhausted")
            return self.sequence.pop(0)

        # 4. Canned result
        if self.result is not None:
            return Result(
                status=self.result.status,
                output=dict(self.result.output) if self.result.output else None,
                error=self.result.error,
                new_checkpoint=(
                    dict(self.result.new_checkpoint)
                    if self.result.new_checkpoint
                    else None
                ),
            )

        # 5. Default deterministic response based on profile
        return Result(
            status="completed",
            output={
                "simulated": True,
                "worker_id": context.worker.id,
                "profile_version": context.profile.version,
                "role": context.profile.role,
                "task_id": context.task.id,
            },
        )


# Protocol conformance marker (mypy-only)
_: "Adapter" = SimulationAdapter()  # type: ignore[assignment]
