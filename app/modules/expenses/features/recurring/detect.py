"""Finding what repeats in the user's entries: the same thing (by name and kind) at a steady
pace and a steady amount. Pure functions over entries already read; tuned in
data/planning.yaml (``recurring.detect``)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from statistics import median_low

from app.modules.expenses.domain import CadenceUnit, EntryKind
from app.modules.expenses.features.entries.models import Entry
from app.modules.expenses.features.products.service import key
from app.modules.expenses.planning import Detect


def group_of(kind: EntryKind, title: str) -> str:
    """What two entries share when they are the same thing; a rule for it shares it too."""
    return f"{kind}|{key(title)}"


@dataclass(frozen=True)
class Repeat:
    """Something that repeats: ``latest`` is its last entry, whose fields a rule would copy."""

    signature: str  # the group and its unit, e.g. "expense|rent|month"
    group: str
    unit: CadenceUnit
    latest: Entry
    amount_minor: int  # the usual amount
    count: int  # how many times it was seen at this pace

    @property
    def last_on(self) -> dt.date:
        return dt.date.fromisoformat(self.latest.date)


def _day(entry: Entry) -> dt.date:
    return dt.date.fromisoformat(entry.date)


def _run(seen: Sequence[Entry], low: int, high: int) -> list[Entry]:
    """The latest of ``seen`` (one per day, oldest first) that are ``low`` to ``high`` days
    apart: the pace as it is now, whatever came before."""
    run = [seen[-1]]
    for entry in reversed(seen[:-1]):
        if not low <= (_day(run[-1]) - _day(entry)).days <= high:
            break
        run.append(entry)
    return run[::-1]


def _steady(run: Sequence[Entry], tolerance: float) -> int | None:
    """The usual amount, if every amount is within ``tolerance`` of it."""
    usual = median_low(entry.amount_minor for entry in run)
    if all(abs(entry.amount_minor - usual) <= tolerance * usual for entry in run):
        return usual
    return None


def _repeat(group: str, seen: Sequence[Entry], today: dt.date, tuning: Detect) -> Repeat | None:
    for unit, (low, high) in tuning.cadences.items():
        run = _run(seen, low, high)
        going = (today - _day(run[-1])).days <= 2 * high  # at most one due date missed since
        if len(run) < tuning.min_occurrences or not going:
            continue
        usual = _steady(run, tuning.amount_tolerance)
        if usual is not None:
            return Repeat(f"{group}|{unit}", group, unit, run[-1], usual, len(run))
    return None


def detect(entries: Iterable[Entry], today: dt.date, tuning: Detect) -> list[Repeat]:
    """What repeats among ``entries`` (oldest first), the most often seen first. Several entries
    of the same thing on one day count once, the last of them."""
    groups: dict[str, dict[str, Entry]] = {}
    for entry in entries:
        groups.setdefault(group_of(entry.kind, entry.title), {})[entry.date] = entry
    found = [
        repeat
        for group, days in groups.items()
        if (repeat := _repeat(group, sorted(days.values(), key=_day), today, tuning))
    ]
    return sorted(found, key=lambda repeat: (-repeat.count, repeat.signature))
