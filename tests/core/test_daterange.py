from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.core.daterange import DateRange, InvalidRangeError, at_local, resolve_range

TODAY = date(2026, 10, 12)
KOLKATA = "Asia/Kolkata"


def resolve(start: date | None = None, end: date | None = None) -> DateRange:
    return resolve_range(start, end, today=TODAY, timezone=KOLKATA, default_days=30, max_days=90)


def test_defaults_to_the_last_days_ending_today() -> None:
    period = resolve()

    assert (period.start, period.end) == (date(2026, 9, 13), TODAY)
    assert len(period.days) == 30


def test_days_are_whole_days_in_the_users_time_zone() -> None:
    period = resolve(date(2026, 10, 1), date(2026, 10, 2))

    # Midnight in Kolkata is 18:30 UTC the evening before; the end is exclusive.
    assert period.begins == datetime(2026, 9, 30, 18, 30, tzinfo=UTC)
    assert period.ends == datetime(2026, 10, 2, 18, 30, tzinfo=UTC)
    assert period.match("at") == {"at": {"$gte": period.begins, "$lt": period.ends}}
    assert period.day_of(datetime(2026, 10, 1, 19, 0, tzinfo=UTC)) == date(2026, 10, 2)


@pytest.mark.parametrize(
    ("day", "timezone", "expected"),
    [
        (date(2026, 10, 20), KOLKATA, datetime(2026, 10, 20, 3, 30, tzinfo=UTC)),
        (date(2026, 10, 30), "America/New_York", datetime(2026, 10, 30, 13, 0, tzinfo=UTC)),
        (date(2026, 11, 2), "America/New_York", datetime(2026, 11, 2, 14, 0, tzinfo=UTC)),
    ],
)
def test_a_local_hour_follows_daylight_saving(day: date, timezone: str, expected: datetime) -> None:
    assert at_local(day, timezone, 9) == expected  # 9:00 local, before and after 1 November


def test_previous_is_the_same_length_just_before() -> None:
    previous = resolve(date(2026, 10, 6), date(2026, 10, 12)).previous()

    assert (previous.start, previous.end) == (date(2026, 9, 29), date(2026, 10, 5))


def test_an_open_start_counts_back_from_the_given_end() -> None:
    period = resolve(end=date(2026, 8, 31))

    assert (period.start, period.end) == (date(2026, 8, 2), date(2026, 8, 31))


@pytest.mark.parametrize(
    ("start", "end", "reason"),
    [
        (date(2026, 10, 12), date(2026, 10, 1), "from_after_to"),
        (date(2026, 1, 1), date(2026, 10, 1), "too_long"),
    ],
)
def test_rejects_impossible_ranges(start: date, end: date, reason: str) -> None:
    with pytest.raises(InvalidRangeError) as error:
        resolve(start, end)

    assert error.value.details["reason"] == reason
