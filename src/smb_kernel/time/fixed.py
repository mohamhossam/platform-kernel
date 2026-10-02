"""Deterministic clock for tests."""

from __future__ import annotations

from datetime import datetime


class FixedClock:
    """Returns a fixed instant, optionally advanced by the test."""

    def __init__(self, instant: datetime) -> None:
        self._instant = instant

    def now(self) -> datetime:
        return self._instant

    def set(self, instant: datetime) -> None:
        """Move the clock, so a test can observe a later timestamp."""
        self._instant = instant
