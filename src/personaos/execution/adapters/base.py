"""Adapter Protocol — the runtime contract.

Per ADR 0005: zero PersonaOS domain logic lives in adapters. The runner
calls adapter.execute(task, checkpoint) -> Result and nothing else.

Concrete adapters (openclaw.py now; local.py, cloud.py later) implement
this Protocol.

TODO(v0.1): define typing.Protocol with:
  - execute(task: Task, checkpoint: Checkpoint) -> Awaitable[Result]

Plus the Result dataclass:
  - status: Literal["completed", "failed", "needs_input"]
  - output: dict | None
  - error: str | None
  - new_checkpoint: State | None  # for resumption
"""
