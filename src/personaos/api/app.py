"""FastAPI application factory — the control plane.

The API does NOT execute tasks. That's the worker's job.

Mounts routes from api/routes/:
  /health, /workers, /nodes, /tasks, /events
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import structlog
import uvicorn
from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from personaos.api.routes import (
    events_router,
    health_router,
    nodes_router,
    tasks_router,
    workers_router,
)
from personaos.config import settings
from personaos.db.session import make_engine, make_session_factory
from personaos.execution.kernel import StaleWorkerError, TaskStatusError
from personaos.logging import configure_logging
from personaos.registry.profile import (
    ProfileVersionExistsError,
    ProfileVersionNotFoundError,
    WorkerNotFoundError,
)

logger = structlog.get_logger(__name__)


def create_app(
    *,
    engine: AsyncEngine | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> FastAPI:
    """Create and configure the FastAPI control plane application."""
    owns_engine = False
    if session_factory is None or engine is None:
        engine = make_engine(settings.database_url)
        session_factory = make_session_factory(engine)
        owns_engine = True

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "control_plane_started",
            host=settings.api_host,
            port=settings.api_port,
        )
        yield
        if owns_engine and engine is not None:
            logger.info("disposing_database_engine")
            await engine.dispose()

    app = FastAPI(
        title="PersonaOS Control Plane",
        description=(
            "PersonaOS v0.1 — Worker, Node, Task, Scheduler, Checkpoint, "
            "Event control plane API."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.engine = engine
    app.state.session_factory = session_factory

    # CORS configuration
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Structured request logging middleware
    @app.middleware("http")
    async def log_requests(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        start_time = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start_time) * 1000
        logger.info(
            "http_request",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round(duration_ms, 2),
        )
        return response

    # Exception handlers mapping domain errors to HTTP semantics
    @app.exception_handler(WorkerNotFoundError)
    async def worker_not_found_handler(
        request: Request, exc: WorkerNotFoundError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": str(exc)},
        )

    @app.exception_handler(ProfileVersionNotFoundError)
    async def profile_not_found_handler(
        request: Request, exc: ProfileVersionNotFoundError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": str(exc)},
        )

    @app.exception_handler(ProfileVersionExistsError)
    async def profile_exists_handler(
        request: Request, exc: ProfileVersionExistsError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": str(exc)},
        )

    @app.exception_handler(TaskStatusError)
    async def task_status_error_handler(
        request: Request, exc: TaskStatusError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": str(exc)},
        )

    @app.exception_handler(StaleWorkerError)
    async def stale_worker_error_handler(
        request: Request, exc: StaleWorkerError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": str(exc)},
        )

    # Register routes
    app.include_router(health_router)
    app.include_router(workers_router)
    app.include_router(nodes_router)
    app.include_router(tasks_router)
    app.include_router(events_router)

    return app


# Default app instance for ASGI servers (uvicorn personaos.api.app:app)
app = create_app()


def main() -> None:
    """Console script entrypoint for personaos-api."""
    configure_logging()
    uvicorn.run(
        "personaos.api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
        access_log=False,  # Handled by our structured logging middleware
    )


if __name__ == "__main__":
    main()
