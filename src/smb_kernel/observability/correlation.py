"""The correlation ID of the unit of work the current thread or task is serving.

Set by the HTTP middleware for a request and by the job worker for a job, and
read by the log formatter, so every log line written while serving it can be
joined to the request's `X-Request-ID` or to the job without passing the ID
through every call.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_CURRENT: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def current_correlation_id() -> str | None:
    return _CURRENT.get()


@contextmanager
def correlation_scope(correlation_id: str) -> Iterator[None]:
    token = _CURRENT.set(correlation_id)
    try:
        yield
    finally:
        _CURRENT.reset(token)
