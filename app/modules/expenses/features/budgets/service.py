"""Budgets (U7): a limit on spending for each week or month, or for a trip's dates. What was
spent is worked out from the entries with the list's own filters, so a budget's figure and the
entries it opens always agree. A budget warns once when it is nearly spent and once when it is
over, in each window and for each limit (data/planning.yaml)."""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import ClassVar

from app.modules.expenses.domain import MODULE, EntryKind
from app.modules.expenses.features.analytics.schemas import Span
from app.modules.expenses.features.analytics.service import Analytics
from app.modules.expenses.features.budgets.models import (
    Budget,
    BudgetPeriod,
    BudgetRepository,
    BudgetScope,
)
from app.modules.expenses.features.budgets.schemas import (
    BudgetIn,
    BudgetOut,
    BudgetPatch,
    BudgetState,
    BudgetStatus,
)
from app.modules.expenses.features.categories.service import Categories
from app.modules.expenses.features.entries.models import Entry
from app.modules.expenses.features.entries.schemas import EntryFilters
from app.modules.expenses.features.insights.schemas import Period
from app.modules.expenses.features.insights.service import bounds
from app.modules.expenses.planning import planning
from app.platform import NEWEST_FIRST, ActorContext, ConflictError, Inbox, PageParams, patched

PERCENT = 100
PERMILLE = 1000  # the warning share is compared in thousandths, so the sums stay in integers
WARNED = (BudgetState.NEAR, BudgetState.OVER)


class BudgetLimitError(ConflictError):
    code: ClassVar[str] = "EXPENSE_BUDGET_LIMIT"
    default_message = "You have as many budgets as you can. Delete one you no longer need."


def window(budget: Budget, today: dt.date) -> Span:
    """The days a budget counts today: its own dates, else the week or month today is in."""
    if budget.period is BudgetPeriod.CUSTOM and budget.start and budget.end:
        return Span(
            start=dt.date.fromisoformat(budget.start), end=dt.date.fromisoformat(budget.end)
        )
    return bounds(Period(budget.period.value), today)


def _level(spent: int, limit: int, threshold: float) -> BudgetState:
    if spent >= limit:
        return BudgetState.OVER
    warn_at = round(threshold * PERMILLE)
    if warn_at and spent * PERMILLE >= warn_at * limit:
        return BudgetState.NEAR
    return BudgetState.OK


def _clamp(days: int, most: int) -> int:
    return min(max(days, 0), most)


def _holds(span: Span, *days: dt.date) -> bool:
    return all(span.start <= day <= span.end for day in days)


def standing(budget: Budget, span: Span, spent: int, today: dt.date) -> BudgetStatus:
    """Where ``spent`` leaves the budget on ``today``. Days are whole days of the window: today
    counts as a day left, and an even pace has spent today's share by the end of it."""
    limit, days = budget.limit_minor, (span.end - span.start).days + 1
    elapsed = _clamp((today - span.start).days + 1, days)
    days_left = _clamp((span.end - today).days + 1, days)
    left = limit - spent
    if today < span.start:
        state = BudgetState.UPCOMING
    elif today > span.end:
        state = BudgetState.ENDED
    else:
        state = _level(spent, limit, budget.alert_threshold)
    return BudgetStatus(
        start=span.start,
        end=span.end,
        spent_minor=spent,
        left_minor=left,
        pct=spent * PERCENT // limit,
        state=state,
        days_left=days_left,
        per_day_left_minor=max(left, 0) // days_left if days_left else 0,
        pace_minor=limit * elapsed // days,
    )


class Budgets:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.repo = BudgetRepository(ctx.scope, ctx.clock)
        self.analytics = Analytics(ctx)
        self.categories = Categories(ctx.scope, ctx.clock)
        self.inbox = Inbox(ctx.scope, ctx.clock, ctx.settings)

    # ------------------------------------------------------------------ reads

    async def status(self, budget: Budget) -> BudgetStatus:
        """How the budget stands today. Spent is what the entries list totals with the same
        filters (its category or tag, and the window's days), in the budget's own currency:
        its limit is in that currency, even after the user changes their base currency."""
        today = self.ctx.today()
        span = window(budget, today)
        filters = EntryFilters.model_validate(
            {"category_id": budget.category_id, "tag": budget.tag}
        )
        spent = (await self.analytics.totals(span, filters, budget.currency)).spent_minor
        return standing(budget, span, spent, today)

    async def overview(self, params: PageParams) -> tuple[list[BudgetOut], int]:
        """The user's budgets, newest first, each with how it stands today."""
        budgets, total = await self.repo.list(params, NEWEST_FIRST)
        found = await asyncio.gather(*(self.status(budget) for budget in budgets))
        return [BudgetOut.of(*pair) for pair in zip(budgets, found, strict=True)], total

    # ------------------------------------------------------------------ writes

    async def _usable(self, body: BudgetIn) -> None:
        if body.category_id is not None:
            await self.categories.ensure_usable(body.category_id, EntryKind.EXPENSE)

    async def _checked(self, budget: Budget) -> BudgetStatus:
        """How the budget stands, warning if that is near or over its limit."""
        status = await self.status(budget)
        await self._alert(budget, status)
        return status

    async def create(self, body: BudgetIn) -> BudgetOut:
        most = planning().budgets.max_per_user
        if await Budget.find(self.repo.query()).count() >= most:
            raise BudgetLimitError(details={"max": most})
        await self._usable(body)
        fields = {**body.model_dump(mode="json"), "currency": self.ctx.user.base_currency}
        budget = await self.repo.create(fields)
        return BudgetOut.of(budget, await self._checked(budget))

    async def update(self, budget_id: str, body: BudgetPatch) -> BudgetOut:
        current = await self.repo.get(budget_id)
        checked = patched(BudgetIn, current, body)  # its scope and period still fit together
        # A budget keeps the category it was made with, even archived (as entries do).
        if checked.category_id != current.category_id:
            await self._usable(checked)
        budget = await self.repo.update(budget_id, checked.model_dump(mode="json"))
        return BudgetOut.of(budget, await self._checked(budget))

    async def delete(self, budget_id: str) -> None:
        await self.repo.soft_delete(budget_id)

    # ------------------------------------------------------------------ entries

    async def observe(self, entry: Entry) -> None:
        """After an expense is saved: warn about each budget it counts in (in its currency:
        overall, its category or one of its tags) that is under way with the entry's day in its
        window, if the entry brings it near or over its limit."""
        if entry.kind is not EntryKind.EXPENSE:
            return
        counted_in = {
            "currency": entry.base_currency or entry.currency,  # what the entry counts in
            "$or": [
                {"scope": BudgetScope.OVERALL},
                {"scope": BudgetScope.CATEGORY, "category_id": entry.category_id},
                {"scope": BudgetScope.TAG, "tag": {"$in": entry.tags}},
            ],
        }
        day, today = dt.date.fromisoformat(entry.date), self.ctx.today()
        found = await Budget.find(self.repo.query(counted_in)).to_list()
        await asyncio.gather(
            *(
                self._checked(budget)
                for budget in found
                if _holds(window(budget, today), day, today)
            )
        )

    async def _alert(self, budget: Budget, status: BudgetStatus) -> None:
        """Tell the user, once, of the highest level reached (over, else near), only while the
        budget is under way: one that has ended or not started yet stays quiet."""
        level = status.state
        if level not in WARNED:
            return
        # Once per budget, level, window and limit: the next week or month, or a new limit,
        # warns again.
        told = f"{status.start}:{budget.limit_minor}"
        if budget.alerted.get(level) == told:
            return
        await self.inbox.notify(
            f"{MODULE}.budget_{level}",
            module=MODULE,
            dedupe_key=f"{MODULE}.budget_{level}:{budget.id}:{told}",
            budget_id=str(budget.id),
            name=budget.name,
            spent_minor=status.spent_minor,
            limit_minor=budget.limit_minor,
            currency=budget.currency,
            pct=status.pct,
        )
        # Told first, then marked: saves at the same moment are deduped by the notification,
        # and the mark keeps it told after the notification is purged.
        await Budget.get_pymongo_collection().update_one(
            {"_id": budget.id, f"alerted.{level}": {"$ne": told}},
            {"$set": {f"alerted.{level}": told}},
        )
