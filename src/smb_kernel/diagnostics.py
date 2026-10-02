"""Single-file JSON Lines trace for explicit local debugging.

The trace deliberately keeps text prompts and final structured completions when
enabled. Credentials, binary payloads, image data URLs, and hidden reasoning are
always redacted. Normal runtime uses the null implementation and performs no I/O.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import traceback
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import IO, Protocol

from pydantic import BaseModel

_REDACTED_KEYS = {
    "api_key",
    "authorization",
    "cookie",
    "credential",
    "database_url",
    "password",
    "secret",
    "token",
}
_REDACTED_SUFFIXES = ("_api_key", "_credential", "_database_url", "_password", "_secret", "_token")


class DebugTrace(Protocol):
    """Write structured diagnostic events to one configured sink."""

    @property
    def enabled(self) -> bool: ...

    @property
    def path(self) -> str | None: ...

    def record(self, event: str, **details: object) -> None: ...

    def close(self) -> None: ...


class NullDebugTrace:
    """No-I/O trace used unless a developer explicitly opts in."""

    @property
    def enabled(self) -> bool:
        return False

    @property
    def path(self) -> None:
        return None

    def record(self, event: str, **details: object) -> None:
        del event, details

    def close(self) -> None:
        return None


class JsonLinesDebugTrace:
    """Thread-safe, flush-on-write trace that appends to one JSONL file."""

    def __init__(self, path: str, secrets: tuple[str, ...] = ()) -> None:
        self._secrets = tuple(
            sorted((secret for secret in secrets if secret), key=len, reverse=True)
        )
        resolved = Path(path).expanduser().resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        self._path = resolved
        self._session_id = str(uuid.uuid4())
        self._lock = threading.Lock()
        self._stream: IO[str] = resolved.open("a", encoding="utf-8")
        self._logging_handler = _TraceLoggingHandler(self)
        logging.getLogger().addHandler(self._logging_handler)
        self.record("trace.session_started", trace_path=str(resolved))

    @property
    def enabled(self) -> bool:
        return True

    @property
    def path(self) -> str:
        return str(self._path)

    def record(self, event: str, **details: object) -> None:
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "session_id": self._session_id,
            "event": event,
            "details": _sanitize(details),
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        for secret in self._secrets:
            line = line.replace(json.dumps(secret, ensure_ascii=False)[1:-1], "[REDACTED]")
        line = re.sub(r"(https?://|postgres(?:ql)?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", line)
        line = re.sub(
            r"(?i)([?&](?:api_key|token|password|secret)=)[^&\s\"]+", r"\1[REDACTED]", line
        )
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()

    def close(self) -> None:
        logging.getLogger().removeHandler(self._logging_handler)
        with self._lock:
            if not self._stream.closed:
                self._stream.close()


def build_debug_trace(*, enabled: bool, path: str, secrets: tuple[str, ...] = ()) -> DebugTrace:
    """Build the configured trace without creating a file when disabled."""
    return JsonLinesDebugTrace(path, secrets) if enabled else NullDebugTrace()


class _TraceLoggingHandler(logging.Handler):
    """Mirror emitted Python log records into the active JSONL trace."""

    def __init__(self, trace: JsonLinesDebugTrace) -> None:
        super().__init__()
        self._trace = trace

    def emit(self, record: logging.LogRecord) -> None:
        exception_chain = None
        if record.exc_info is not None:
            exception_chain = "".join(traceback.format_exception(*record.exc_info))
        self._trace.record(
            "python.log",
            logger=record.name,
            level=record.levelname,
            message=record.getMessage(),
            exception_chain=exception_chain,
        )


def _sanitize(value: object, key: str | None = None) -> object:
    normalized_key = (key or "").casefold()
    if normalized_key in _REDACTED_KEYS or normalized_key.endswith(_REDACTED_SUFFIXES):
        return "[REDACTED]"
    if normalized_key == "reasoning":
        return "[REDACTED: hidden reasoning]"
    if isinstance(value, bytes):
        digest = hashlib.sha256(value).hexdigest()
        return f"[REDACTED: {len(value)} binary bytes; sha256={digest}]"
    if isinstance(value, str) and value.startswith("data:") and ";base64," in value:
        media_type = value[5:].split(";", 1)[0]
        return f"[REDACTED: {media_type} data URL]"
    if isinstance(value, BaseModel):
        return _sanitize(value.model_dump(mode="json"), key)
    if isinstance(value, Enum):
        return _sanitize(value.value, key)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(item_key): _sanitize(item, str(item_key)) for item_key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_sanitize(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return repr(value)
