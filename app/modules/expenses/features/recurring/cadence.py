"""When a repeating rule falls due. Every due date is worked out from the anchor, never from the
one before, so a month's day is kept after a shorter month: 31 Jan, 28 (or 29) Feb, 31 Mar."""

from __future__ import annotations

import calendar
import datetime as dt

from pydantic import BaseModel

from app.modules.expenses.domain import CadenceUnit

MONTHS = 12
DAYS = {CadenceUnit.DAY: 1, CadenceUnit.WEEK: 7}


class Cadence(BaseModel):
    """Every ``every`` days, weeks, months or years, on the days that follow from ``anchor``."""

    unit: CadenceUnit
    every: int
    anchor: str  # YYYY-MM-DD, one of its due dates (a rule's first)

    @property
    def start(self) -> dt.date:
        return dt.date.fromisoformat(self.anchor)


def _months(cadence: Cadence) -> int:
    return cadence.every * (MONTHS if cadence.unit is CadenceUnit.YEAR else 1)


def nth(cadence: Cadence, n: int) -> dt.date:
    """The ``n``-th due date; the anchor is the 0th."""
    start = cadence.start
    if cadence.unit in DAYS:
        return start + dt.timedelta(days=DAYS[cadence.unit] * cadence.every * n)
    month = start.month - 1 + _months(cadence) * n
    year, month = start.year + month // MONTHS, month % MONTHS + 1
    return dt.date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def _near(cadence: Cadence, day: dt.date) -> int:
    """The number of a due date at or just before ``day`` (never before the anchor)."""
    start = cadence.start
    if cadence.unit in DAYS:
        return max((day - start).days // (DAYS[cadence.unit] * cadence.every), 0)
    months = (day.year - start.year) * MONTHS + day.month - start.month
    return max(months // _months(cadence) - 1, 0)


def on_or_after(cadence: Cadence, day: dt.date) -> dt.date:
    """The first due date on ``day`` or later."""
    n = _near(cadence, day)
    while (due := nth(cadence, n)) < day:
        n += 1
    return due


def after(cadence: Cadence, day: dt.date) -> dt.date:
    """The first due date after ``day``."""
    return on_or_after(cadence, day + dt.timedelta(days=1))


def falls_on(cadence: Cadence, day: dt.date) -> bool:
    """Is ``day`` one of its due dates?"""
    return on_or_after(cadence, day) == day


def between(cadence: Cadence, first: dt.date, last: dt.date) -> list[dt.date]:
    """Its due dates from ``first`` to ``last``, both included."""
    found: list[dt.date] = []
    due = on_or_after(cadence, first)
    while due <= last:
        found.append(due)
        due = after(cadence, due)
    return found
