"""WorkerOrchestrator — polling loop that drives the existing kernel.

Per ADR 0012 — Worker Orchestrator (Step 7).

The orchestrator's job is intentionally tiny:

    discover queued task
        ↓
    kernel.execute(task)
        ↓
    kernel's existing atomic claim (Phase 3)

That's the whole pipeline. The orchestrator does NOT:

  - introduce a new claim API (the kernel's ``UPDATE WHERE status='queued'``
    is the atomic claim)
  - introduce a new state machine
  - introduce leases, failover, or ``execution_node`` (deferred to v0.2)
  - bypass the kernel (no parallel execution path)
  - know what the Adapter does (no OpenClaw references)

Concurrency boundary (the critical invariant):

  Two orchestrators calling ``process_one()`` on the same queued task →
  one succeeds (the kernel's atomic claim transitions ``queued →
  in_progress``) and the other catches ``TaskStatusError`` (the kernel's
  Phase 3 found ``rowcount=0`` because the first orchestrator already
  won the race). ``TaskStatusError`` is treated as a *claim conflict*,
  NOT as a task failure — ``claim_conflicts`` is incremented, no error
  propagates to the run() loop.

Stop semantics:

  ``stop()`` just sets a flag. The current ``process_one()`` is allowed
  to complete before the loop checks the flag and exits. No new
  execution path; no signal handling; no cancellation of in-flight work.
"""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from personaos.execution.kernel import TaskStatusError, WorkerExecutionKernel
from personaos.orchestrator.metrics import OrchestratorMetrics
from personaos.workload.service import WorkloadService


class WorkerOrchestrator:
    """Polling loop that drives the existing ``WorkerExecutionKernel``.

    Per ADR 0012: does not own claim semantics, context resolution, or
    adapter protocol — those all live in the kernel + registry. The
    orchestrator's only job is find + invoke + handle the concurrency
    boundary.

    Polls at ``poll_interval`` seconds when the queue is empty. Returns
    immediately to the loop when work exists (tight loop when busy).
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        kernel: WorkerExecutionKernel,
        poll_interval: float = 0.1,
    ) -> None:
        """Build the orchestrator.

        Args:
            session: AsyncSession used to SELECT queued tasks.
                The same session is shared with the kernel — the
                orchestrator's SELECT and the kernel's atomic UPDATE
                happen in the same SQLAlchemy session/transaction.
            kernel: WorkerExecutionKernel to invoke per task.
                Receives the found task; its Phase 3 is the atomic claim.
            poll_interval: Seconds to sleep when the queue is empty.
                Default 0.1 (10 Hz). The default is bounded so a
                misconfigured orchestrator can't spin a CPU.
        """
        self._session = session
        self._kernel = kernel
        self._poll_interval = poll_interval
        self._stop_requested = False
        self._running = False
        self._metrics = OrchestratorMetrics()

    async def run(self) -> None:
        """Poll until ``stop()`` is called.

        Tight loop when work exists, sleeps ``poll_interval`` when the
        queue is empty. Returns when ``stop()`` has been called AND the
        current iteration (process_one + optional sleep) completes.

        Exceptions from ``kernel.execute()`` other than ``TaskStatusError``
        propagate to the caller. ``TaskStatusError`` is the concurrency
        boundary — caught inside ``process_one()`` and recorded as
        ``claim_conflicts``, NOT as a failure.
        """
        self._running = True
        try:
            while not self._stop_requested:
                self._metrics.poll_iterations += 1
                processed = await self.process_one()
                if not processed and not self._stop_requested:
                    await asyncio.sleep(self._poll_interval)
        finally:
            self._running = False

    async def process_one(self) -> bool:
        """One iteration: find + invoke + handle the concurrency boundary.

        Per ADR 0013 (Step 8): delegates the "next runnable candidate"
        query to ``WorkloadService.next_runnable()`` instead of doing
        a raw ``SELECT ... WHERE status='queued'``. The workload layer
        owns the runnable predicate (status='queued' AND dependencies
        satisfied). The orchestrator still doesn't claim; the kernel's
        Phase 3 is the atomic claim.

        Returns True if a task was processed (``kernel.execute()``
        returned successfully). Returns False if no runnable work was
        found (empty queue OR every queued task has unsatisfied deps)
        OR a ``TaskStatusError`` was caught (concurrency conflict —
        another orchestrator won the race for the same task).

        The atomic claim is the kernel's Phase 3
        (``UPDATE tasks SET status='in_progress' WHERE id=? AND status='queued'``).
        This method does NOT introduce any new claim path.
        """
        # 1. Check for stale in-progress tasks needing reclaim/recovery
        workload = WorkloadService(self._session)
        stale_tasks = await workload.find_stale_in_progress(limit=1)
        if stale_tasks:
            stale = stale_tasks[0]
            if stale.side_effect_classification == "IDEMPOTENT":
                try:
                    await self._kernel.reclaim(stale)
                    self._metrics.tasks_processed += 1
                    return True
                except TaskStatusError:
                    self._metrics.claim_conflicts += 1
                    return False
            else:
                routed = await workload.route_stale_to_review(
                    stale,
                    reason=(
                        "side-effect safety: auto-reclaim blocked "
                        f"({stale.side_effect_classification})"
                    ),
                )
                if routed:
                    self._metrics.tasks_processed += 1
                    return True
                else:
                    self._metrics.claim_conflicts += 1
                    return False

        # 2. Find one runnable candidate (delegates to WorkloadService).
        #    Per ADR 0013: status='queued' AND dependencies satisfied.
        #    Read-only — kernel still owns the atomic claim.
        task = await workload.next_runnable()
        if task is None:
            return False

        # 3. Pass to the kernel. The kernel's Phase 3 is the atomic
        #    claim — same mechanism as direct kernel invocation, no
        #    parallel claim path introduced here.
        try:
            await self._kernel.execute(task)
        except TaskStatusError:
            # Concurrency boundary: another orchestrator's kernel
            # already transitioned this task to 'in_progress' (or
            # 'completed') between our next_runnable() and our
            # kernel's UPDATE. This is a CLAIM CONFLICT, not a task
            # failure — record it and move on.
            self._metrics.claim_conflicts += 1
            return False

        self._metrics.tasks_processed += 1
        return True

    def stop(self) -> None:
        """Request clean shutdown.

        Sets a flag that the ``run()`` loop checks at the top of each
        iteration and after each ``asyncio.sleep``. The current
        ``process_one()`` (if any) is allowed to complete before the
        loop exits — no cancellation of in-flight work.
        """
        self._stop_requested = True

    @property
    def is_running(self) -> bool:
        """True if the ``run()`` loop is currently active.

        False before ``run()`` starts and after it exits. Transitions
        happen inside ``run()``'s try/finally block, so this property
        accurately reflects whether the loop is currently executing.
        """
        return self._running

    @property
    def metrics(self) -> OrchestratorMetrics:
        """Read-only access to the in-memory metrics counters.

        The returned object is the same instance the orchestrator
        mutates, so counter values are live (not snapshots).
        """
        return self._metrics
