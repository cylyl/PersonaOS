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
import dataclasses

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from personaos.db.models import Task as TaskModel
from personaos.execution.kernel import TaskStatusError, WorkerExecutionKernel
from personaos.execution.result import Result
from personaos.orchestrator.metrics import OrchestratorMetrics
from personaos.workload.scheduler import WorkloadScheduler
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
        worker_id: str | None = None,
        node_id: str | None = None,
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
            worker_id: Optional worker identity for skill/capability scheduling.
            node_id: Optional node identity for node-affinity scheduling.
        """
        self._session = session
        self._kernel = kernel
        self._poll_interval = poll_interval
        self._worker_id = worker_id
        self._node_id = node_id
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
                    # Transition to completed if recovery execution produced a result
                    row = (
                        await self._session.execute(
                            select(TaskModel).where(
                                TaskModel.id == stale.id,
                                TaskModel.generation == self._kernel._generation,
                            )
                        )
                    ).scalar_one_or_none()
                    if row and row.status == "in_progress" and row.result is not None:
                        stale_reclaimed = dataclasses.replace(
                            stale,
                            checkpoint_ref=None,
                            generation=self._kernel._generation,
                        )
                        await self._kernel.complete(
                            stale_reclaimed, Result(status="completed", output=row.result)
                        )
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

        # 2. Find one runnable candidate.
        #    Per ADR 0019 (Step 16): if worker_id is available, delegate to
        #    WorkloadScheduler matching skills, capabilities, affinity, node, and priority.
        #    If worker_id is unset, fall back to WorkloadService.next_runnable().
        #    Read-only — kernel still owns the atomic claim.
        effective_worker_id = self._worker_id or getattr(self._kernel, "_worker_id", None)
        effective_node_id = self._node_id or getattr(self._kernel, "_node_id", None)

        if effective_worker_id:
            scheduler = WorkloadScheduler(self._session)
            task = await scheduler.find_candidate(
                worker_id=effective_worker_id,
                node_id=effective_node_id,
            )
        else:
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
