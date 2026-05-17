from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any, Literal, cast

import structlog

LogFormat = Literal["json", "console"]

_SECRET_SUBSTRINGS: tuple[str, ...] = (
    "token",
    "cookie",
    "authorization",
    "password",
    "secret",
    "api_key",
    "apikey",
    "credential",
    "auth",
)


def _redact_secrets(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict.keys()):
        lk = key.lower()
        if any(needle in lk for needle in _SECRET_SUBSTRINGS):
            event_dict[key] = "***"
    return event_dict


def configure_logging(level: str = "INFO", fmt: LogFormat = "json") -> None:
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )

    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact_secrets,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if fmt == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty()))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger = structlog.get_logger(name) if name else structlog.get_logger()
    return cast(structlog.stdlib.BoundLogger, logger)
