"""OrchestratorMetrics — in-memory counters for ``WorkerOrchestrator``.

Per ADR 0012 — Worker Orchestrator.

Three counters, all in-memory. Process restart loses them. Persistent
metrics / OpenTelemetry integration is deferred to v0.2.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class OrchestratorMetrics:
    """In-memory counters for one ``WorkerOrchestrator`` instance.

    Attributes:
        tasks_processed: ``kernel.execute()`` returned successfully
            (the task transitioned ``queued → in_progress → completed|failed``
            via the existing kernel lifecycle).
        claim_conflicts: ``TaskStatusError`` caught from ``kernel.execute()``
            (a competing orchestrator won the race for the same task —
            this is the concurrency boundary, NOT a task failure).
        poll_iterations: ``run()`` loop passes (incremented once per pass,
            regardless of whether work was found or whether ``stop()``
            was called mid-iteration).
    """

    tasks_processed: int = 0
    claim_conflicts: int = 0
    poll_iterations: int = 0
