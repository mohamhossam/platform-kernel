"""Wall-clock adapter."""

from __future__ import annotations

from datetime import UTC, datetime


class SystemClock:
    """Reads the real time, in UTC."""

    def now(self) -> datetime:
        return datetime.now(tz=UTC)
