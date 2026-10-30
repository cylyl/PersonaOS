"""Worker process entry point.

Run as: `personaos-worker` or `python -m personaos.worker_main`

Separate OS process from the API (per the kernel architecture in VIKUNJA.md).
On startup:
  1. Register this node binding (worker_nodes)
  2. Start heartbeat loop
  3. Enter the runner main loop via WorkerRunner and WorkerOrchestrator
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import socket
import sys

import structlog

from personaos.config import settings
from personaos.db.session import make_engine, make_session_factory
from personaos.execution.adapters.base import Adapter
from personaos.execution.adapters.litellm import LiteLLMAdapter, LiteLLMConfig
from personaos.execution.adapters.openclaw import OpenClawAdapter
from personaos.execution.adapters.simulation import SimulationAdapter
from personaos.execution.kernel import WorkerExecutionKernel
from personaos.execution.runner import WorkerRunner
from personaos.logging import configure_logging
from personaos.orchestrator.worker import WorkerOrchestrator

logger = structlog.get_logger(__name__)


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    """Parse worker CLI arguments."""
    parser = argparse.ArgumentParser(
        description="PersonaOS Worker Process",
        prog="personaos-worker",
    )
    parser.add_argument(
        "--worker-id",
        type=str,
        default=os.environ.get("PERSONAOS_WORKER_ID", "worker-devops-001"),
        help="Worker identifier (default: $PERSONAOS_WORKER_ID or worker-devops-001)",
    )
    parser.add_argument(
        "--node-id",
        type=str,
        default=os.environ.get("PERSONAOS_NODE_ID", socket.gethostname()),
        help="Node identifier (default: $PERSONAOS_NODE_ID or hostname)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=settings.worker_poll_interval_seconds,
        help="Queue poll interval in seconds",
    )
    parser.add_argument(
        "--adapter",
        type=str,
        default=settings.runtime_adapter,
        help="Runtime adapter (openclaw, simulation, litellm)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=settings.litellm_model,
        help=f"Model name for litellm adapter (default: {settings.litellm_model})",
    )
    parser.add_argument(
        "--api-base",
        type=str,
        default=settings.litellm_api_base,
        help=f"API base URL for litellm adapter (default: {settings.litellm_api_base})",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=settings.litellm_api_key,
        help="API key for litellm adapter (default: from settings or env)",
    )
    parser.add_argument(
        "--concurrency",
        "--max-concurrency",
        dest="concurrency",
        type=int,
        default=None,
        help="Maximum concurrent tasks (default: worker.max_concurrent_tasks)",
    )
    parser.add_argument(
        "--heartbeat-interval",
        type=float,
        default=30.0,
        help="Worker heartbeat interval in seconds (default: 30.0)",
    )
    parser.add_argument(
        "--drain-timeout",
        type=float,
        default=30.0,
        help="Graceful drain timeout in seconds when shutting down (default: 30.0)",
    )
    return parser.parse_args(args)


async def async_main(args: argparse.Namespace) -> None:
    """Async main routine running the worker lifecycle."""
    logger.info(
        "worker_process_starting",
        worker_id=args.worker_id,
        node_id=args.node_id,
        adapter=args.adapter,
        poll_interval=args.poll_interval,
        concurrency=args.concurrency,
        heartbeat_interval=args.heartbeat_interval,
        drain_timeout=args.drain_timeout,
    )

    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)

    # Initialize runtime adapter
    adapter: Adapter
    if args.adapter == "simulation":
        adapter = SimulationAdapter()
    elif args.adapter == "litellm":
        adapter = LiteLLMAdapter(
            api_base=args.api_base,
            api_key=args.api_key,
            model=args.model,
        )
    else:
        adapter = OpenClawAdapter()

    async with session_factory() as orchestrator_session:
        kernel = WorkerExecutionKernel(
            session=orchestrator_session,
            adapter=adapter,
            worker_id=args.worker_id,
            node_id=args.node_id,
        )
        orchestrator = WorkerOrchestrator(
            session=orchestrator_session,
            kernel=kernel,
            poll_interval=args.poll_interval,
            worker_id=args.worker_id,
            node_id=args.node_id,
            session_factory=session_factory,
            max_concurrency=args.concurrency,
        )
        runner = WorkerRunner(
            orchestrator=orchestrator,
            session_factory=session_factory,
            worker_id=args.worker_id,
            node_id=args.node_id,
            heartbeat_interval=args.heartbeat_interval,
            drain_timeout=args.drain_timeout,
        )

        try:
            await runner.run()
        finally:
            await engine.dispose()

    logger.info("worker_process_stopped", worker_id=args.worker_id)


def main() -> None:
    """Console script entrypoint for personaos-worker."""
    configure_logging()
    args = parse_args(sys.argv[1:])
    with contextlib.suppress(KeyboardInterrupt, SystemExit):
        asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
