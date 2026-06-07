"""
Structured logging via structlog.

pretty mode (default, development): coloured, human-readable output
json  mode (production):            one JSON object per line, machine-parseable

Controlled by the LOG_FORMAT env var ("pretty" | "json").
"""
from __future__ import annotations

import logging
import structlog
from structlog.types import Processor


def _build_processors(fmt: str) -> list[Processor]:
    shared: list[Processor] = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if fmt == "json":
        shared.append(structlog.processors.JSONRenderer())
    else:
        shared.append(structlog.dev.ConsoleRenderer(colors=True))

    return shared


def _configure(fmt: str) -> None:
    logging.basicConfig(
        format="%(message)s",
        level=logging.INFO,
    )
    structlog.configure(
        processors=_build_processors(fmt),
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """
    Return a structured logger bound to *name*.

    Usage:
        log = get_logger(__name__)
        log.info("email.triaged", email_id="abc", priority="urgent")
    """
    # Import settings lazily to avoid circular imports at module load time
    from agent.config import settings

    # Only configure once per process — structlog is idempotent but
    # re-configuring during tests wastes time.
    if not _configure.called:  # type: ignore[attr-defined]
        _configure(settings.log_format)
        _configure.called = True  # type: ignore[attr-defined]

    return structlog.get_logger(name)


# Initialise the sentinel attribute
_configure.called = False  # type: ignore[attr-defined]
