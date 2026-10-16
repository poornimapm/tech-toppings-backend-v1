"""What the command palette finds in expenses: saved views by name, loans by person, products by
name, and entries by title, place or note. Each kind gets a fair share of the hits, so many
matching entries never hide the one view or loan the user was looking for."""

from __future__ import annotations

import asyncio
import datetime as dt
import re
from collections.abc import Sequence
from itertools import chain, zip_longest
from typing import Any

from app.modules.expenses.features.entries.models import EntryRepository
from app.modules.expenses.features.entries.schemas import EntryFilters
from app.modules.expenses.features.ious.models import IouRepository
from app.modules.expenses.features.products.models import ProductRepository
from app.modules.expenses.features.saved_filters.models import SavedFilterRepository
from app.platform import NEWEST_FIRST, ActorContext, PageParams, SearchHit, search


def _like(text: str) -> dict[str, Any]:
    return {"$regex": re.escape(text), "$options": "i"}


def _day(value: str | None) -> dt.date | None:
    return dt.date.fromisoformat(value) if value else None


def _fair(found: Sequence[list[SearchHit]], limit: int) -> list[SearchHit]:
    """Turn about from each kind, in the kinds' order, up to ``limit``."""
    rounds = zip_longest(*found)
    return [hit for hit in chain.from_iterable(rounds) if hit is not None][:limit]


async def _views(ctx: ActorContext, text: str, limit: int) -> list[SearchHit]:
    repo = SavedFilterRepository(ctx.scope, ctx.clock)
    found, _ = await repo.list(PageParams(1, limit), [("name_key", 1)], {"name": _like(text)})
    return [
        SearchHit(kind="saved_filter", id=str(view.id), title=view.name, path=f"?view={view.id}")
        for view in found
    ]


async def _loans(ctx: ActorContext, text: str, limit: int) -> list[SearchHit]:
    repo = IouRepository(ctx.scope, ctx.clock)
    # Open loans first (settled_on is null), most recently lent first.
    found, _ = await repo.list(
        PageParams(1, limit), [("settled_on", 1), ("date", -1)], {"counterparty": _like(text)}
    )
    return [
        SearchHit(
            kind=f"loan_{loan.direction}",
            id=str(loan.id),
            title=loan.counterparty,
            path=f"plan?show=loans&focus={loan.id}",
            # Open: what is still owed and when it is due; paid back: when that was.
            amount_minor=None if loan.settled_on else loan.amount_minor - loan.settled_minor,
            currency=loan.currency,
            date=_day(loan.settled_on or loan.due_on),
        )
        for loan in found
    ]


async def _products(ctx: ActorContext, text: str, limit: int) -> list[SearchHit]:
    repo = ProductRepository(ctx.scope, ctx.clock)
    criteria = {"$or": [{"name": _like(text)}, {"aliases": _like(text)}], "merged_into": None}
    found, _ = await repo.list(PageParams(1, limit), [("name_key", 1)], criteria)
    return [
        SearchHit(kind="product", id=str(item.id), title=item.name, path=f"prices/{item.id}")
        for item in found
    ]


async def _entries(ctx: ActorContext, text: str, limit: int) -> list[SearchHit]:
    repo = EntryRepository(ctx.scope, ctx.clock)
    criteria = EntryFilters(q=text).criteria()
    found, _ = await repo.list(PageParams(1, limit), [("date", -1), *NEWEST_FIRST], criteria)
    return [
        SearchHit(
            kind=f"entry_{entry.kind}",
            id=str(entry.id),
            title=entry.title,
            path=f"entries/{entry.id}",
            amount_minor=entry.amount_minor,
            currency=entry.currency,
            date=_day(entry.date),
        )
        for entry in found
    ]


@search
async def expenses_search(ctx: ActorContext, text: str, limit: int) -> list[SearchHit]:
    kinds = (_views, _loans, _products, _entries)
    found = await asyncio.gather(*(kind(ctx, text, limit) for kind in kinds))
    return _fair(found, limit)
