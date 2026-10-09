"""Time source. Services take a Clock instead of calling datetime.now(), so tests can freeze
and advance time (lockouts, token expiry, sliding sessions) without sleeping."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time, timezone-aware, in UTC."""
        ...


class SystemClock:
    def now(self) -> datetime:
        # MongoDB stores milliseconds; truncating here keeps stored and in-memory values equal.
        current = datetime.now(UTC)
        return current.replace(microsecond=current.microsecond // 1000 * 1000)
