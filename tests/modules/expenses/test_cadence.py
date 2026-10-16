"""When a repeating rule falls due: every due date follows from the anchor, so a month's day is
kept after a shorter month, and a leap day comes back in leap years."""

from __future__ import annotations

from datetime import date

import pytest

from app.modules.expenses.domain import CadenceUnit
from app.modules.expenses.features.recurring.cadence import (
    Cadence,
    after,
    between,
    falls_on,
    nth,
    on_or_after,
)


def cadence(unit: str, anchor: str, every: int = 1) -> Cadence:
    return Cadence(unit=CadenceUnit(unit), every=every, anchor=anchor)


def dates(*days: str) -> list[date]:
    return [date.fromisoformat(day) for day in days]


@pytest.mark.parametrize(
    ("rule", "expected"),
    [
        # The 31st: the last day of a shorter month, then the 31st again.
        (
            cadence("month", "2026-01-31"),
            dates("2026-01-31", "2026-02-28", "2026-03-31", "2026-04-30", "2026-05-31"),
        ),
        (
            cadence("month", "2028-01-31"),
            dates("2028-01-31", "2028-02-29", "2028-03-31", "2028-04-30", "2028-05-31"),
        ),
        # A leap day: 28 February in the other years, and back on the 29th in a leap year.
        (
            cadence("year", "2028-02-29"),
            dates("2028-02-29", "2029-02-28", "2030-02-28", "2031-02-28", "2032-02-29"),
        ),
        # Every three months, from the last day of January.
        (
            cadence("month", "2026-01-31", every=3),
            dates("2026-01-31", "2026-04-30", "2026-07-31", "2026-10-31", "2027-01-31"),
        ),
        (
            cadence("week", "2026-10-05"),
            dates("2026-10-05", "2026-10-12", "2026-10-19", "2026-10-26", "2026-11-02"),
        ),
        (
            cadence("week", "2026-10-05", every=2),
            dates("2026-10-05", "2026-10-19", "2026-11-02", "2026-11-16", "2026-11-30"),
        ),
        (
            cadence("day", "2026-12-30", every=1),
            dates("2026-12-30", "2026-12-31", "2027-01-01", "2027-01-02", "2027-01-03"),
        ),
    ],
)
def test_due_dates_follow_from_the_anchor(rule: Cadence, expected: list[date]) -> None:
    assert [nth(rule, n) for n in range(5)] == expected
    assert between(rule, expected[0], expected[-1]) == expected
    # Stepping from one due date to the next gives the same: nothing drifts.
    stepped = [expected[0]]
    for _ in range(4):
        stepped.append(after(rule, stepped[-1]))
    assert stepped == expected


def test_the_due_date_on_or_after_a_day() -> None:
    rule = cadence("month", "2026-01-31")

    assert on_or_after(rule, date(2026, 2, 28)) == date(2026, 2, 28)
    assert on_or_after(rule, date(2026, 3, 1)) == date(2026, 3, 31)
    assert after(rule, date(2026, 2, 28)) == date(2026, 3, 31)
    # Nothing falls due before the anchor.
    assert on_or_after(rule, date(2020, 5, 5)) == date(2026, 1, 31)
    assert on_or_after(cadence("week", "2026-10-05"), date(2026, 9, 1)) == date(2026, 10, 5)


def test_only_due_dates_fall_due() -> None:
    rule = cadence("month", "2026-01-31")

    assert falls_on(rule, date(2026, 2, 28))
    assert falls_on(rule, date(2026, 4, 30))
    assert not falls_on(rule, date(2026, 3, 30))
    assert not falls_on(rule, date(2025, 12, 31))  # before the anchor
    assert between(rule, date(2026, 3, 1), date(2026, 3, 30)) == []
