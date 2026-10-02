"""Process log configuration: readable text locally, one JSON object per line in production.

Operational logs record what happened, how long it took and the correlation ID.
They never carry requirement text or provider payloads; that is the opt-in
debug trace's job.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from enum import Enum
from typing import TextIO

from smb_kernel.observability.correlation import (
    current_correlation_id,
)


class LogFormat(Enum):
    """Operational log output: readable lines locally, one JSON object per line in production."""

    TEXT = "text"
    JSON = "json"


# Attributes every LogRecord has; anything else was passed through `extra=`.
_RECORD_ATTRIBUTES = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "correlation_id",
    "taskName",
    # uvicorn attaches an ANSI-coloured duplicate of the message.
    "color_message",
}
_TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(correlation_id)s] %(message)s"
# Servers that install their own handlers; routed through ours instead.
_SERVER_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        correlation_id = current_correlation_id()
        if correlation_id:
            payload["correlation_id"] = correlation_id
        payload.update(
            (key, value)
            for key, value in record.__dict__.items()
            if key not in _RECORD_ATTRIBUTES and not key.startswith("_")
        )
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class _CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = current_correlation_id() or "-"
        return True


class _OperationalHandler(logging.StreamHandler[TextIO]):
    """Marks the handler this module installed, so reconfiguring replaces it."""


def configure_logging(level: str, log_format: LogFormat) -> None:
    """Send every log record to stdout in the configured format. Safe to call again."""
    root = logging.getLogger()
    for existing in [item for item in root.handlers if isinstance(item, _OperationalHandler)]:
        root.removeHandler(existing)
    handler = _OperationalHandler(sys.stdout)
    handler.addFilter(_CorrelationFilter())
    handler.setFormatter(
        JsonLogFormatter() if log_format is LogFormat.JSON else logging.Formatter(_TEXT_FORMAT)
    )
    root.addHandler(handler)
    root.setLevel(level)
    for name in _SERVER_LOGGERS:
        server_logger = logging.getLogger(name)
        server_logger.handlers.clear()
        server_logger.propagate = True
