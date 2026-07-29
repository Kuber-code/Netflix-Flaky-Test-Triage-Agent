"""Structured logging.

Logs are JSON on stderr by default so that a CI job's log stream stays
machine-parseable, with a human-readable console renderer available for local
use. stdout is reserved for report output -- a tool whose logs and whose data
share a stream cannot be piped into anything.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Final

import structlog
from structlog.typing import Processor

_DEFAULT_LEVEL: Final = "info"

# Module-level state, held in a dict so that no function needs `global`.
_state: Final[dict[str, bool]] = {"configured": False}


def configure_logging(level: str = _DEFAULT_LEVEL, *, json_output: bool = True) -> None:
    """Configure structlog once per process.

    Repeated calls are cheap no-ops so that library-style use (tests, the eval
    harness) does not stack processors.
    """
    if _state["configured"]:
        return

    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=numeric_level)

    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
    _state["configured"] = True


def get_logger(name: str, **initial: Any) -> structlog.stdlib.BoundLogger:
    """Return a bound logger, configuring logging with defaults if needed."""
    if not _state["configured"]:
        configure_logging()
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name).bind(**initial)
    return logger


def reset_for_testing() -> None:
    """Allow tests to reconfigure logging. Not used in production paths."""
    _state["configured"] = False
    structlog.reset_defaults()
