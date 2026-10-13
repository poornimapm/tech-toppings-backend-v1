"""Agent tools for expenses: thin wrappers over the same services as the API, so the
assistant follows the same rules and sees only the caller's own entries."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from pydantic import Field

from app.modules.expenses.domain import EntryKind, EntrySource
from app.modules.expenses.features.categories.service import Categories
from app.modules.expenses.features.entries.schemas import EntryFilters, EntryIn, EntryOut
from app.modules.expenses.features.entries.service import EntryService
from app.platform import ActorContext, PageParams, to_major, to_minor, tool

Day = Annotated[date | None, Field(description="YYYY-MM-DD")]


def _brief(entry: EntryOut) -> dict[str, Any]:
    return {
        "id": entry.id,
        "kind": entry.kind,
        "title": entry.title,
        "amount": to_major(entry.amount_minor, entry.currency),
        "currency": entry.currency,
        "category_id": entry.category_id,
        "date": entry.date,
        "merchant": entry.merchant,
        "for_whom": entry.for_whom,
    }


@tool("read")
async def expenses_categories(ctx: ActorContext) -> list[dict[str, Any]]:
    """The categories an entry can be filed under: id, kind (expense or income) and name
    (system categories are named by their id, e.g. groceries)."""
    return [
        {"id": item.id, "kind": item.kind, "name": item.name or item.id.replace("_", " ")}
        for item in await Categories(ctx.scope, ctx.clock).all()
        if not item.archived
    ]


@tool("read")
async def expenses_list(
    ctx: ActorContext,
    start: Day = None,
    end: Day = None,
    kind: Annotated[EntryKind | None, Field(description="Leave out for both")] = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
) -> list[dict[str, Any]]:
    """The user's entries, newest first, optionally between two days: title, amount in the
    currency's main unit, category, date, merchant and who it was for."""
    filters = EntryFilters.model_validate({"from": start, "to": end, "kind": kind})
    entries, _ = await EntryService(ctx).entries.list(
        PageParams(1, limit), [("date", -1), ("created_at", -1)], filters.criteria()
    )
    return [_brief(EntryOut.of(entry)) for entry in entries]


@tool("write")
async def expenses_add(  # noqa: PLR0913 - a tool's arguments are its schema
    ctx: ActorContext,
    title: Annotated[str, Field(description="What it was, in the user's words")],
    amount: Annotated[float, Field(gt=0, description="In the currency's main unit, e.g. 250.50")],
    category_id: Annotated[str, Field(description="An id from expenses_categories")],
    *,
    kind: EntryKind = EntryKind.EXPENSE,
    on: Annotated[date | None, Field(description="YYYY-MM-DD; leave out for today")] = None,
    merchant: Annotated[str | None, Field(description="Where it was paid, if said")] = None,
    for_whom: Annotated[str | None, Field(description="Who it was for, if said")] = None,
) -> dict[str, Any]:
    """Record money the user spent (expense) or received (income)."""
    today = ctx.clock.now().astimezone(ZoneInfo(ctx.user.timezone)).date()
    body = EntryIn(
        kind=kind,
        title=title,
        amount_minor=to_minor(amount, ctx.user.base_currency),
        category_id=category_id,
        date=on or today,
        merchant=merchant,
        for_whom=for_whom,
    )
    entry, _ = await EntryService(ctx).create(body, source=EntrySource.ASSISTANT)
    return _brief(EntryOut.of(entry))


@tool("client")
async def expenses_open(ctx: ActorContext) -> None:
    """Show the user their expenses page."""
