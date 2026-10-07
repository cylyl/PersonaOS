"""Runtime adapters — thin shims around external agent runtimes.

Per ADR 0005: zero PersonaOS domain logic lives here. The Adapter Protocol
(base.py) defines the contract: execute(task, checkpoint) -> Result.
Concrete adapters (OpenClaw in v0.1; future local/cloud runtimes) translate
between the runtime API and PersonaOS's domain types.
"""
