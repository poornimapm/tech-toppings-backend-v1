"""The day an expense happened, from words like "yesterday", "last Friday", "3 days ago",
"12/10" or "5 Oct". Expenses are logged after the fact, so a day never lands in the future."""

from __future__ import annotations

import re
from datetime import date, timedelta

from app.modules.expenses.parsing.lexicon import Lexicon, known, lookup
from app.modules.expenses.parsing.numbers import Item, Number, Word
from app.platform import on_or_before

NUMERIC = re.compile(r"^(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?$")
MONTH_DAYS = 31


def _day_number(item: Item | None) -> int | None:
    whole = isinstance(item, Number) and item.value % 1 == 0 and 1 <= item.value <= MONTH_DAYS
    return int(item.value) if whole and isinstance(item, Number) else None


def _at(  # noqa: PLR0911 - one return for each way of saying a day
    items: list[Item], start: int, today: date, lexicon: Lexicon
) -> tuple[int, date | None]:
    """A date starting at ``start``: how many items it takes, and the day."""
    item = items[start]
    following = items[start + 1] if start + 1 < len(items) else None
    after = items[start + 2] if start + 2 < len(items) else None
    if isinstance(item, Number):
        ago = isinstance(following, Word) and known(following.text, lexicon.ago_days)
        if ago and isinstance(after, Word) and known(after.text, lexicon.ago_marks):
            return 3, today - timedelta(days=int(item.value))
        month = lookup(following.text, lexicon.months) if isinstance(following, Word) else None
        if month is not None and (day := _day_number(item)):
            return 2, on_or_before(today, day, month + 1)
        return 0, None
    if numeric := NUMERIC.match(item.text):
        day, month, year = (int(part) if part else None for part in numeric.groups())
        return 1, on_or_before(today, day or 0, month or 0, year)
    if (days_ago := lookup(item.text, lexicon.days)) is not None:
        return 1, today - timedelta(days=days_ago)
    if (weekday := lookup(item.text, lexicon.weekdays)) is not None:
        # The most recent such day; said on that very day, it means a week ago.
        return 1, today - timedelta(days=(today.weekday() - weekday) % 7 or 7)
    month = lookup(item.text, lexicon.months)
    if month is not None and (day := _day_number(following)):
        return 2, on_or_before(today, day, month + 1)
    return 0, None


def extract(items: list[Item], today: date, lexicon: Lexicon) -> tuple[date | None, list[Item]]:
    """The first day mentioned, and the items without the words that said it."""
    for start in range(len(items)):
        length, found = _at(items, start, today, lexicon)
        if found is None:
            continue
        before = items[:start]
        if before and isinstance(before[-1], Word) and known(before[-1].text, lexicon.last):
            before = before[:-1]  # "last" Friday
        return found, before + items[start + length :]
    return None, items
