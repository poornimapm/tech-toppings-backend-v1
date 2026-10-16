"""Saved entries that look like one about to be saved: the same kind and amount on a nearby
day, under the same or a similar name, or at the same place (data/planning.yaml). Advice only:
nothing is ever refused because of it."""

from __future__ import annotations

import asyncio
import datetime as dt
from difflib import SequenceMatcher

from app.modules.expenses.features.entries.models import Entry, EntryRepository
from app.modules.expenses.features.entries.schemas import (
    DuplicateCandidate,
    DuplicateResult,
    DuplicatesIn,
    DuplicatesOut,
    EntryOut,
    on_days,
)
from app.modules.expenses.features.products.service import key
from app.modules.expenses.planning import planning
from app.platform import ActorContext


def _shift(day: dt.date, days: int) -> dt.date:
    """``day`` moved by ``days``, stopping at the calendar's ends instead of failing."""
    try:
        return day + dt.timedelta(days=days)
    except OverflowError:
        return dt.date.max if days > 0 else dt.date.min


def _same_place(saved: str | None, new: str | None) -> bool:
    return saved is not None and new is not None and key(saved) == key(new)


def _alike(entry: Entry, candidate: DuplicateCandidate) -> bool:
    saved, new = key(entry.title), key(candidate.title)
    return (
        saved == new
        or SequenceMatcher(None, saved, new).ratio() >= planning().duplicates.similarity
        or _same_place(entry.merchant, candidate.merchant)
    )


class Duplicates:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.entries = EntryRepository(ctx.scope, ctx.clock)

    async def check(self, body: DuplicatesIn) -> DuplicatesOut:
        found = await asyncio.gather(*(self._matches(item) for item in body.candidates))
        return DuplicatesOut(
            results=[
                DuplicateResult(index=index, matches=[EntryOut.of(entry) for entry in matches])
                for index, matches in enumerate(found)
            ]
        )

    async def _matches(self, candidate: DuplicateCandidate) -> list[Entry]:
        tuning = planning().duplicates
        days = tuning.window_days
        criteria = {
            "kind": candidate.kind,
            "amount_minor": candidate.amount_minor,
            "currency": self.ctx.user.base_currency,
            **on_days(_shift(candidate.date, -days), _shift(candidate.date, days)),
        }
        nearby = await Entry.find(self.entries.query(criteria)).sort("-date", "-_id").to_list()
        alike = [
            entry
            for entry in nearby
            if str(entry.id) != candidate.exclude_id and _alike(entry, candidate)
        ]
        return alike[: tuning.max_results]
