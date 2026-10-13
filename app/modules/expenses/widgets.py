"""Dashboard widgets: spending and income for the period, in the user's base currency."""

from __future__ import annotations

from app.modules.expenses.domain import EntryKind
from app.modules.expenses.features.entries.models import EntryRepository
from app.modules.expenses.features.entries.schemas import on_days
from app.modules.expenses.features.entries.service import sums
from app.modules.expenses.stats import total
from app.platform import ActorContext, DateRange, Kpi, Point, Series, to_major, widget


async def _kpi(ctx: ActorContext, period: DateRange, kind: EntryKind) -> Kpi:
    currency, earlier = ctx.user.base_currency, period.previous()
    return Kpi(
        value=await total(ctx.scope, ctx.clock, kind, currency, period.start, period.end),
        previous=await total(ctx.scope, ctx.clock, kind, currency, earlier.start, earlier.end),
        format="currency",
        currency=currency,
    )


@widget("kpi")
async def expenses_spent(ctx: ActorContext, period: DateRange) -> Kpi:
    return await _kpi(ctx, period, EntryKind.EXPENSE)


@widget("kpi")
async def expenses_received(ctx: ActorContext, period: DateRange) -> Kpi:
    return await _kpi(ctx, period, EntryKind.INCOME)


@widget("series")
async def expenses_spent_daily(ctx: ActorContext, period: DateRange) -> Series:
    currency = ctx.user.base_currency
    criteria = EntryRepository(ctx.scope, ctx.clock).query(
        {"kind": EntryKind.EXPENSE, "currency": currency, **on_days(period.start, period.end)}
    )
    spent = {row["date"]: row["amount_minor"] for row in await sums(criteria, "date")}
    return Series(
        points=[
            Point(day=day, value=to_major(spent.get(day.isoformat(), 0), currency))
            for day in period.days
        ],
        format="currency",
        currency=currency,
    )
