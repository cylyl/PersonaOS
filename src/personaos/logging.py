"""Structured JSON logging via structlog.

One configure_logging() call at process startup (api/app.py, worker_main.py).
Every module uses structlog.get_logger(__name__).
"""

from __future__ import annotations

import logging
import sys

import structlog

from personaos.config import settings


def configure_logging(
    log_level: str | None = None,
    log_format: str | None = None,
) -> None:
    """Configure structured logging for PersonaOS processes."""
    level_name = (log_level or settings.log_level).upper()
    fmt = (log_format or settings.log_format).lower()

    numeric_level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=numeric_level)

    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if fmt == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
