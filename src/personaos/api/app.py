"""FastAPI application factory — the control plane.

The API does NOT execute tasks. That's the worker's job.

Mounts routes from api/routes/:
  /health, /workers, /nodes, /tasks, /events

TODO(v0.1): implement create_app() that wires FastAPI, mounts routes,
configures CORS (tight by default), dependency injection (api/deps.py),
and structured logging middleware. Export `app = create_app()` for
uvicorn, plus a `main()` entrypoint for the personaos-api console script.
"""
