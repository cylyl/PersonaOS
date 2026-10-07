"""Worker process entry point.

Run as: `uv run python -m personaos.worker_main`

Separate OS process from the API (per the kernel architecture in VIKUNJA.md).
On startup:
  1. Register this node (emits node.joined)
  2. Start heartbeat loop
  3. Enter the runner main loop

TODO(v0.1): wire up node registration + heartbeat + execution.runner.run()
in a single asyncio task tree.
"""
