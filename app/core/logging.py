"""Structured logging.

structlog renders JSON (production) or readable console output (development) to stdout only:
Render's disk is ephemeral, so the platform collects stdout. Standard-library loggers (uvicorn,
pymongo) are routed through the same processors, so every line has one shape.

Every event passes through :class:`Redactor`, which masks values whose key looks sensitive.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable, Mapping, MutableMapping
from typing import Any, TextIO

import structlog
from structlog.types import EventDict, Processor, WrappedLogger

from app.core.config import LogFormat, LogSettings

REDACTED = "***"
_QUIET_LOGGERS = ("pymongo", "uvicorn.access", "httpx", "httpcore")


class Redactor:
    """structlog processor that masks values of sensitive keys at any nesting depth."""

    def __init__(self, keys: Iterable[str]) -> None:
        self._keys = tuple(key.lower() for key in keys)

    def __call__(self, _logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
        return self._scrub_mapping(event_dict)

    def _is_sensitive(self, key: str) -> bool:
        lowered = key.lower()
        return any(fragment in lowered for fragment in self._keys)

    def _scrub(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return self._scrub_mapping(value)
        if isinstance(value, list | tuple):
            return type(value)(self._scrub(item) for item in value)
        return value

    def _scrub_mapping(self, mapping: Mapping[str, Any]) -> MutableMapping[str, Any]:
        return {
            key: REDACTED if self._is_sensitive(str(key)) else self._scrub(value)
            for key, value in mapping.items()
        }


class StdoutHandler(logging.StreamHandler[TextIO]):
    """Writes to whatever ``sys.stdout`` is *now* (reloaders and test capture swap it)."""

    @property
    def stream(self) -> TextIO:
        return sys.stdout

    @stream.setter
    def stream(self, _value: TextIO) -> None:
        """Ignore the stream StreamHandler.__init__ assigns; always resolve dynamically."""


def configure_logging(settings: LogSettings) -> None:
    """Configure structlog and the standard library once, at app creation."""
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        Redactor(settings.redact_keys),
        structlog.processors.StackInfoRenderer(),
    ]
    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if settings.format is LogFormat.JSON
        else structlog.dev.ConsoleRenderer()
    )
    if settings.format is LogFormat.JSON:
        shared.append(structlog.processors.format_exc_info)

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    handler = StdoutHandler()
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
        )
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(settings.level)

    # uvicorn installs its own handlers; route them through ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name)
    return logger
