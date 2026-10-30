"""A period of whole days in the user's time zone: the one way dashboards, widgets and (later)
reports turn ``?from=&to=`` into a database filter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from fastapi import status

from app.core.errors import AppError


class InvalidRangeError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code: ClassVar[str] = "RANGE_INVALID"
    default_message = "That date range isn't valid."


CENTURY = 2000  # a two-digit year is this century's


def on_or_before(today: date, day: int, month: int, year: int | None = None) -> date | None:
    """That day and month, in the year given or else the latest one that isn't in the future;
    None for a day no calendar has (31 February)."""
    try:
        if year is not None:
            return date(year + CENTURY if year < 100 else year, month, day)  # noqa: PLR2004
        found = date(today.year, month, day)
        return found if found <= today else date(today.year - 1, month, day)
    except ValueError:
        return None


def at_local(day: date, timezone: str, hour: int = 0) -> datetime:
    """``hour`` o'clock on ``day`` in ``timezone``, as a UTC instant (DST included)."""
    return datetime.combine(day, time(hour), ZoneInfo(timezone)).astimezone(UTC)


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date  # inclusive
    timezone: str

    def _at(self, day: date) -> datetime:
        return at_local(day, self.timezone)

    @property
    def begins(self) -> datetime:
        return self._at(self.start)

    @property
    def ends(self) -> datetime:
        """Exclusive: the first instant after the last day."""
        return self._at(self.end + timedelta(days=1))

    @property
    def days(self) -> list[date]:
        return [self.start + timedelta(days=n) for n in range((self.end - self.start).days + 1)]

    def previous(self) -> DateRange:
        """The period of the same length just before this one (for trends)."""
        length = timedelta(days=len(self.days))
        return DateRange(self.start - length, self.end - length, self.timezone)

    def match(self, field: str = "created_at") -> dict[str, Any]:
        return {field: {"$gte": self.begins, "$lt": self.ends}}

    def day_of(self, moment: datetime) -> date:
        return moment.astimezone(ZoneInfo(self.timezone)).date()


def resolve_range(
    start: date | None,
    end: date | None,
    *,
    today: date,
    timezone: str,
    default_days: int,
    max_days: int,
) -> DateRange:
    """Missing bounds default to the last ``default_days`` days ending today."""
    try:
        end = end or today
        start = start or end - timedelta(days=default_days - 1)
        if start > end:
            raise InvalidRangeError(details={"reason": "from_after_to"})
        if (end - start).days + 1 > max_days:
            raise InvalidRangeError(details={"reason": "too_long", "max_days": max_days})
        span = DateRange(start, end, timezone)
        span.previous().begins, span.ends
    except OverflowError as exc:
        raise InvalidRangeError(details={"reason": "out_of_range"}) from exc
    return span
