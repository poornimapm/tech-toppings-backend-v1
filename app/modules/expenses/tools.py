"""Agent tools for expenses: thin wrappers over the same services as the API, so the
assistant follows the same rules and sees only the caller's own entries.

The assistant never writes an entry itself. It hands over drafts (``expenses_draft``); the
user checks them on a confirm card and saves them through the API like any other entry.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.expenses.domain import EntryKind
from app.modules.expenses.features.categories.service import Categories
from app.modules.expenses.features.entries.models import Entry, EntryRepository
from app.modules.expenses.features.entries.schemas import EntryFilters, EntryOut
from app.modules.expenses.features.entries.service import EntryService
from app.platform import ActorContext, PageParams, to_major, tool

Day = Annotated[date | None, Field(description="YYYY-MM-DD")]
MAX_DRAFTS = 10


class DraftIn(BaseModel):
    """An entry as understood from what the user said. Leave out what they didn't say."""

    model_config = ConfigDict(extra="forbid")

    kind: EntryKind = EntryKind.EXPENSE
    title: Annotated[
        str | None, Field(max_length=120, description="What it was, in their words")
    ] = None
    amount: Annotated[
        float | None, Field(gt=0, description="In the currency's main unit, e.g. 250.50")
    ] = None
    category_id: Annotated[str | None, Field(description="An id from expenses_categories")] = None
    date: Annotated[
        str | None,
        Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="YYYY-MM-DD; leave out for today"),
    ] = None
    merchant: Annotated[str | None, Field(max_length=80, description="Where it was paid")] = None
    for_whom: Annotated[str | None, Field(max_length=60, description="Who it was for")] = None
    uncertain: Annotated[
        list[Literal["kind", "title", "amount", "category_id", "date"]],
        Field(description="Fields you guessed, for the user to check"),
    ] = []


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
async def expenses_habits(
    ctx: ActorContext, limit: Annotated[int, Field(ge=1, le=200)] = 100
) -> list[dict[str, Any]]:
    """What the user logs most often and the category they file it under: title, kind,
    category_id and how many times. Prefer these categories for the same things."""
    criteria = EntryRepository(ctx.scope, ctx.clock).query()
    by = {name: f"${name}" for name in ("title", "kind", "category_id")}
    cursor = await Entry.get_pymongo_collection().aggregate(
        [
            {"$match": criteria},
            {"$group": {"_id": by, "times": {"$sum": 1}}},
            {"$sort": {"times": -1, "_id.title": 1}},
            {"$limit": limit},
        ]
    )
    return [{**row["_id"], "times": row["times"]} async for row in cursor]


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


@tool("client")
async def expenses_draft(
    ctx: ActorContext,
    drafts: Annotated[list[DraftIn], Field(min_length=1, max_length=MAX_DRAFTS)],
) -> None:
    """Show the user draft entries to check and save: one for each thing they spent on or
    received. Nothing is saved until they do."""
    categories = Categories(ctx.scope, ctx.clock)
    for draft in drafts:
        if draft.category_id is not None:
            await categories.ensure_usable(draft.category_id, draft.kind)


@tool("client")
async def expenses_open(ctx: ActorContext) -> None:
    """Show the user their expenses page."""
