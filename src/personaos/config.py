"""Application configuration (pydantic-settings).

Reads from environment variables and .env. One Settings instance per process,
imported by api/app.py, worker_main.py, scripts/dev/seed.py.

TODO(v0.1): define DATABASE_URL, API_HOST, API_PORT, LOG_LEVEL,
WORKER_POLL_INTERVAL_SECONDS, TASK_LEASE_TTL_SECONDS, RUNTIME_ADAPTER.
"""
