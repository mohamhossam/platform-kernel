"""Clock port.

Time is an external dependency. Provenance and staleness both record it, so it
is injected rather than read from a use case, keeping every timestamp
assertion in the suite deterministic.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class ClockPort(Protocol):
    """Outbound port for reading the current time."""

    def now(self) -> datetime:
        """Return the current time as a timezone-aware UTC datetime."""
        ...
