"""Structured JSON logging via structlog.

One configure_logging() call at process startup (api/app.py, worker_main.py).
Every module uses structlog.get_logger(__name__).

TODO(v0.1): bind context vars (worker_id, task_id, node_id) so each log line
carries the running task's identity.
"""
