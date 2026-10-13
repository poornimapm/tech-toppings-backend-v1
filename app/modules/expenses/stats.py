"""Figures about a user's money that the platform shows (Welcome tile, dashboard)."""

from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

from app.modules.expenses.domain import EntryKind
from app.modules.expenses.features.entries.models import EntryRepository
from app.modules.expenses.features.entries.schemas import on_days
from app.modules.expenses.features.entries.service import sums
from app.platform import Clock, Scope, TileStat, User, to_major


async def total(
    scope: Scope, clock: Clock, kind: EntryKind, currency: str, start: date, end: date
) -> float:
    """What was spent (or received) on the days given, in the currency's main unit."""
    criteria = EntryRepository(scope, clock).query(
        {"kind": kind, "currency": currency, **on_days(start, end)}
    )
    return to_major(sum(row["amount_minor"] for row in await sums(criteria)), currency)


async def month_total(scope: Scope, clock: Clock) -> TileStat | None:
    """Spent so far this month, by the user's own calendar."""
    user = await User.get(scope.owner_id)
    if user is None:
        return None
    today = clock.now().astimezone(ZoneInfo(user.timezone)).date()
    spent = await total(
        scope, clock, EntryKind.EXPENSE, user.base_currency, today.replace(day=1), today
    )
    return TileStat(
        value=spent, format="currency", currency=user.base_currency, label="month_total"
    )
