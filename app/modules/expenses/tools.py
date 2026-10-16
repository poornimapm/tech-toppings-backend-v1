"""Agent tools for expenses: thin wrappers over the same services as the API, so the
assistant follows the same rules and sees only the caller's own entries.

The assistant never writes anything itself. It hands over drafts (``expenses_draft``), loans
(``expenses_iou_draft``) and product matches (``expenses_product_matches``); the user checks
them and saves them through the API like anything else.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from app.modules.expenses.domain import EntryKind
from app.modules.expenses.features.categories.service import Categories
from app.modules.expenses.features.entries.models import Entry, EntryRepository
from app.modules.expenses.features.entries.schemas import EntryFilters, EntryOut
from app.modules.expenses.features.entries.service import EntryService
from app.modules.expenses.features.insights.schemas import Period
from app.modules.expenses.features.insights.service import Insights
from app.modules.expenses.features.ious.models import IouDirection, IouRepository
from app.modules.expenses.features.ious.schemas import IouState
from app.modules.expenses.features.ious.service import Ious, SettlementInvalidError
from app.modules.expenses.features.products.service import MAX_PRODUCTS, Products
from app.modules.expenses.units import known_unit, units
from app.platform import ActorContext, PageParams, to_major, tool

Day = Annotated[date | None, Field(description="YYYY-MM-DD")]
DAY_TEXT = r"^\d{4}-\d{2}-\d{2}$"  # a day as the model writes it; checked again when saved
MAX_DRAFTS = 10
MAX_MATCHES = 20


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
        str | None, Field(pattern=DAY_TEXT, description="YYYY-MM-DD; leave out for today")
    ] = None
    quantity: Annotated[
        float | None, Field(gt=0, description='How much was bought (2 in "2 kg"), with unit')
    ] = None
    unit: Annotated[
        Annotated[str, AfterValidator(known_unit)] | None,
        Field(description=f"The quantity's unit: one of {', '.join(units())}"),
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


def _in_major(value: Any, currency: str) -> Any:
    """``{"spent_minor": 25050}`` -> ``{"spent": 250.5}``, all the way down: money as people
    say it, so the model never has to convert paise."""
    if isinstance(value, dict):
        return {
            (name[: -len("_minor")] if name.endswith("_minor") else name): (
                to_major(item, currency)
                if name.endswith("_minor") and isinstance(item, int)
                else _in_major(item, currency)
            )
            for name, item in value.items()
        }
    if isinstance(value, list):
        return [_in_major(item, currency) for item in value]
    return value


@tool("read")
async def expenses_insight_facts(
    ctx: ActorContext,
    period: Annotated[Literal["week", "month"], Field(description="Monday to Sunday, or a month")],
    date: Annotated[date, Field(description="Its first day (YYYY-MM-DD)")],
) -> dict[str, Any]:
    """The figures of a week or month so far, against the same days of the one before: spent,
    received and kept (net), top categories with their change, the top place, people, the
    biggest and unusual expenses, a month-end forecast and price changes. Amounts are in the
    currency's main unit; `partial` means the period isn't over."""
    facts = await Insights(ctx).facts(Period(period), date)
    return dict(_in_major(facts, facts["currency"]))


class MatchIn(BaseModel):
    """Products and titles that are the same thing as one product."""

    model_config = ConfigDict(extra="forbid")

    product_id: Annotated[str, Field(description="The product to keep, from expenses_products")]
    products: Annotated[
        list[str], Field(max_length=MAX_DRAFTS, description="Ids of products to combine into it")
    ] = []
    titles: Annotated[
        list[str], Field(max_length=MAX_DRAFTS, description="Titles from expenses_items it covers")
    ] = []


@tool("read")
async def expenses_products(ctx: ActorContext) -> list[dict[str, Any]]:
    """The products the user follows: id, name, the names it goes by (aliases), what it is
    measured by (mass, volume or count), how many purchases, and the ids of products the user
    said are different things (apart)."""
    found, _ = await Products(ctx).overview(PageParams(1, MAX_PRODUCTS))
    fields = {"id", "name", "aliases", "dimension", "purchases", "apart"}
    return [item.model_dump(include=fields) for item in found]


@tool("read")
async def expenses_items(
    ctx: ActorContext, limit: Annotated[int, Field(ge=1, le=200)] = 100
) -> list[dict[str, Any]]:
    """What the user buys that no product follows yet: title and how many times, most often
    bought first."""
    return await Products(ctx).unclaimed(limit)


@tool("client")
async def expenses_product_matches(
    ctx: ActorContext,
    matches: Annotated[list[MatchIn], Field(min_length=1, max_length=MAX_MATCHES)],
) -> None:
    """Show the user products and titles that look like the same thing, to combine. Nothing
    changes until they confirm each match."""
    products = Products(ctx)
    for match in matches:
        for product_id in [match.product_id, *match.products]:
            await products.repo.get(product_id)


class IouDraftIn(BaseModel):
    """Money lent or borrowed, or paid back on such a loan, as understood from what the user
    said. Leave out what they didn't say."""

    model_config = ConfigDict(extra="forbid")

    action: Annotated[
        Literal["new", "repay"],
        Field(description="new: money lent or borrowed; repay: money paid back on a loan"),
    ] = "new"
    direction: Annotated[
        IouDirection,
        Field(
            description="lent: the user gave the money (it is owed to them); borrowed: they "
            "took it (they owe it). For repay, the direction of the loan being paid back"
        ),
    ]
    counterparty: Annotated[
        str | None, Field(max_length=60, description="The other person, in the user's words")
    ] = None
    amount: Annotated[
        float | None, Field(gt=0, description="In the currency's main unit, e.g. 500")
    ] = None
    date: Annotated[
        str | None, Field(pattern=DAY_TEXT, description="YYYY-MM-DD; leave out for today")
    ] = None
    due_on: Annotated[
        str | None,
        Field(pattern=DAY_TEXT, description="new: when it is to be paid back, only if said"),
    ] = None
    note: Annotated[str | None, Field(max_length=1000)] = None
    iou_id: Annotated[
        str | None, Field(description="repay: the loan it pays back, from expenses_ious")
    ] = None
    uncertain: Annotated[
        list[Literal["action", "direction", "counterparty", "amount", "date", "due_on"]],
        Field(description="Fields you guessed, for the user to check"),
    ] = []

    @model_validator(mode="after")
    def loan_only_for_repay(self) -> IouDraftIn:
        if self.iou_id is not None and self.action != "repay":
            raise ValueError("iou_id is only for a repayment")
        return self


@tool("client")
async def expenses_iou_draft(
    ctx: ActorContext,
    drafts: Annotated[list[IouDraftIn], Field(min_length=1, max_length=MAX_DRAFTS)],
) -> None:
    """Show the user loans to check and save: money they lent or borrowed, or money paid back
    on a loan. Nothing is saved until they do."""
    loans = IouRepository(ctx.scope, ctx.clock)
    for draft in drafts:
        if draft.iou_id is None:
            continue
        loan = await loans.get(draft.iou_id)
        # A payment on a loan the other way round, or on one paid back already, can't be saved:
        # said now, the AI can correct it before the user sees it.
        if loan.direction != draft.direction or loan.settled_on is not None:
            reason = "settled" if loan.settled_on is not None else "direction"
            outstanding = loan.amount_minor - loan.settled_minor
            raise SettlementInvalidError(
                details={"reason": reason, "outstanding_minor": outstanding}
            )


@tool("read")
async def expenses_ious(
    ctx: ActorContext,
    person: Annotated[
        str | None, Field(max_length=60, description="Only loans with this person")
    ] = None,
    state: Annotated[
        IouState, Field(description="open (not paid back in full), settled, or all")
    ] = IouState.OPEN,
    limit: Annotated[int, Field(ge=1, le=50)] = 20,
) -> list[dict[str, Any]]:
    """The user's loans, most recently lent first: id, the other person (counterparty),
    direction (lent: owed to the user; borrowed: the user owes it), the amount, what was paid
    back (settled) and what is still outstanding, in the currency's main unit, the day it was
    lent and when it is due."""
    found, _ = await Ious(ctx).overview(PageParams(1, limit), state, person)
    fields = {
        "id", "counterparty", "direction", "amount_minor", "settled_minor", "outstanding_minor",
        "currency", "date", "due_on", "settled_on",
    }  # fmt: skip
    return [
        _in_major(item.model_dump(mode="json", include=fields), item.currency) for item in found
    ]
