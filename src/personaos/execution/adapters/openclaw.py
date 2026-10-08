"""OpenClaw adapter — thin shim around the OpenClaw runtime.

Per ADR 0005: only translates between OpenClaw's API and PersonaOS's domain
types. No domain logic, no policy logic, no scheduler logic.

Per ADR 0009: receives a frozen WorkerExecutionContext (worker + snapshot
profile + task), not a bare task. The profile is the historical contract.

Per ADR 0010: returns a Result. The kernel handles success/failure persistence.

If OPENCLAW_API_URL is unset, raise a clear config error on first use.
The adapter is the only place that knows OpenClaw's wire format.

The actual HTTP /run call lands in Step 6 (agent loop + LLM provider +
checkpoint I/O). Step 5 only establishes the Result signature — so the
type system enforces that adapters consume the snapshot, not live state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from personaos.execution.context import WorkerExecutionContext
from personaos.execution.result import Result


if TYPE_CHECKING:
    from personaos.execution.adapters.base import Adapter


class OpenClawAdapter:
    """OpenClaw runtime adapter (stub for Step 4/5; full impl in Step 6)."""

    def __init__(self, api_url: str | None = None) -> None:
        self._api_url = api_url

    async def execute(
        self,
        context: WorkerExecutionContext,
        checkpoint: Any | None = None,
    ) -> Result:
        """Execute the task via OpenClaw's /run endpoint.

        TODO(Step 6): implement the actual HTTP call.
        Translates context.task.input + context.profile (snapshot) into
        OpenClaw's wire format. Parses response into Result.
        """
        raise NotImplementedError(
            "OpenClawAdapter.execute lands in Step 6; "
            "Step 5 only establishes the Result signature."
        )


# Protocol conformance marker (mypy-only)
_: "Adapter" = OpenClawAdapter  # type: ignore[type-arg,assignment]
