"""PersonaOS worker orchestrator.

Per ADR 0012 — Worker Orchestrator (Step 7).

The orchestrator is a polling loop that drives the existing
``WorkerExecutionKernel``. It does NOT introduce:

  - a new claim API (the kernel's Phase 3 atomic UPDATE is the claim)
  - a new task state machine
  - leases / failover / execution_node (deferred to v0.2)
  - a parallel execution path (orchestrator calls kernel.execute only)

Static invariants (enforced by tests/integration/test_worker_orchestrator.py):

  1. No ``OpenClawAdapter`` / ``openclaw`` / ``OPENCLAW_API_URL`` references
     in worker.py — the orchestrator is adapter-agnostic.
  2. ``WorkerExecutionKernel`` is the only execution surface — no
     bypass paths.
  3. No v0.2 identifiers (``execution_node`` / ``lease`` / ``failover`` /
     ``rebalance``) in worker.py — deferred to v0.2 per ADR 0008 future
     evolution.
"""