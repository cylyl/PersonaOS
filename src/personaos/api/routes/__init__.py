"""API routes — workers, nodes, tasks, health, events.

v0.1 surface:
  /health                       — DB connectivity
  /workers                      — CRUD + profile versioning + quarantine
  /nodes                        — worker-node binding, drain, heartbeat
  /tasks                        — enqueue, review queue, inspect, approve, reject
  /events                       — read event log
"""

from personaos.api.routes.events import router as events_router
from personaos.api.routes.health import router as health_router
from personaos.api.routes.nodes import router as nodes_router
from personaos.api.routes.tasks import router as tasks_router
from personaos.api.routes.workers import router as workers_router

__all__ = [
    "events_router",
    "health_router",
    "nodes_router",
    "tasks_router",
    "workers_router",
]
