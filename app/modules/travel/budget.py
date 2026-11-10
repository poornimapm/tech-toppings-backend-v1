"""A trip's budget: estimated against actual, by category, in the trip's currency, and who owes
whom. Each expense is converted through the platform's rates (``Fx``) on its own day: today's for
one with no day or a day still to come, as no rate is known before its day. Only a rate of that
very day counts. One whose rate can't be had is listed as unconverted and counted nowhere: no rate
is ever guessed, and an older day's isn't taken for it (``Fx`` answers the latest it keeps when
its provider can't be reached).

Once the provider fails, budgets don't wait on it again for a while (``budget.fx_rest_seconds``):
until then they use only the rates already kept, so a load with many days to convert waits on it
at most once, and those after it not at all. A failure is told by ``Fx`` raising, answering an
older day's rate, or answering today's as kept from before its cache's time (``Fx`` asks again
after that time, so it did and couldn't). A rate that can't be had just now is still said to be
one there is none of when the provider doesn't have the cost's currency or the trip's, so waiting
is no use: it said so before, or no rates it gave ever named it (each names all it has). This
holds for the lookup that finds it failing as for those while it rests, so a reason never turns
on which came first. The rest is this process's own memory, as the AI providers' rest is.

Who owes whom is worked out in whole minor units from what was really spent and who paid it. An
expense is split evenly among those who share it (the payer alone, when nobody does), and the minor
units an even split leaves over go one each to the first of them in the trip's order of travellers.
Then the largest debt is paid to the largest credit, again and again, ties going by that order: the
same records always give the same transfers, and never more of them than travellers less one."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Literal

from app.modules.travel.models import ExpenseKind, ExpenseRepository, Trip, TripExpense
from app.modules.travel.rules import rules
from app.modules.travel.schemas import (
    TravelBalanceOut,
    TravelBudgetOut,
    TravelCategoryOut,
    TravelTransferOut,
    TravelUnconvertedOut,
)
from app.modules.travel.trips import Trips
from app.platform import ActorContext, Fx, FxRate, FxUnavailableError, FxUnsupportedError, convert

NoRate = Literal["unavailable", "unsupported"]
Converted = list[tuple[TripExpense, int]]  # each expense, in the trip's currency


class ProviderRest:
    """Until when budgets don't ask the rates provider again, once it has failed one; and the
    currencies it is known not to have."""

    def __init__(self) -> None:
        self.until: dt.datetime | None = None
        self.unsupported: set[str] = set()

    def on(self, now: dt.datetime) -> bool:
        return self.until is not None and now < self.until

    def begin(self, now: dt.datetime) -> None:
        self.until = now + dt.timedelta(seconds=rules().budget.fx_rest_seconds)

    def reset(self) -> None:
        self.until = None
        self.unsupported.clear()


PROVIDER_REST = ProviderRest()


def _aware(moment: dt.datetime) -> dt.datetime:
    """MongoDB gives datetimes back without a time zone; they are UTC."""
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)


async def _unsupported(base: str) -> bool:
    """Whether the provider doesn't have ``base``: it said so, or none of the rates kept, each
    naming every currency it has, names it (with none kept, nothing is known)."""
    if base in PROVIDER_REST.unsupported:
        return True
    named = {"$or": [{"base": base}, {f"rates.{base}": {"$exists": True}}]}
    if await FxRate.find_one({}) is None or await FxRate.find_one(named) is not None:
        return False
    PROVIDER_REST.unsupported.add(base)
    return True


def split(amount: int, sharers: Sequence[str]) -> dict[str, int]:
    """``amount`` among ``sharers``, in their order: evenly, and what is left over a unit each to
    the first of them, so the parts always add up to it."""
    each, left = divmod(amount, len(sharers))
    return {who: each + (1 if place < left else 0) for place, who in enumerate(sharers)}


def settle(balances: Mapping[str, int], order: Sequence[str]) -> list[TravelTransferOut]:
    """Transfers that bring every balance to nothing. They add up to nothing, so each round pays
    one of them off in full."""
    left = {who: balances.get(who, 0) for who in order}
    transfers: list[TravelTransferOut] = []
    while True:
        owing = [who for who in order if left[who] < 0]
        owed = [who for who in order if left[who] > 0]
        if not owing or not owed:
            return transfers
        payer = min(owing, key=lambda who: left[who])  # the first of the largest, by order
        payee = max(owed, key=lambda who: left[who])
        amount = min(-left[payer], left[payee])
        transfers.append(
            TravelTransferOut(from_traveller_id=payer, to_traveller_id=payee, amount_minor=amount)
        )
        left[payer] += amount
        left[payee] -= amount


def _categories(converted: Converted) -> list[TravelCategoryOut]:
    """Each category with something in it, in the order data/travel.yaml lists them."""
    totals: dict[str, dict[ExpenseKind, int]] = {}
    for expense, amount in converted:
        line = totals.setdefault(expense.category, dict.fromkeys(ExpenseKind, 0))
        line[expense.kind] += amount
    known = rules().budget.categories

    def place(name: str) -> tuple[int, str]:
        return (known.index(name) if name in known else len(known), name)

    return [
        TravelCategoryOut(
            category=name,
            estimated_minor=totals[name][ExpenseKind.ESTIMATE],
            actual_minor=totals[name][ExpenseKind.ACTUAL],
        )
        for name in sorted(totals, key=place)
    ]


def _shared(trip: Trip, converted: Converted) -> tuple[dict[str, int], dict[str, int]]:
    """What each traveller paid and what their part of it is, of what was really spent and has a
    payer."""
    order = [traveller.id for traveller in trip.travellers]
    paid, share = dict.fromkeys(order, 0), dict.fromkeys(order, 0)
    for expense, amount in converted:
        payer = expense.paid_by
        if expense.kind is not ExpenseKind.ACTUAL or payer is None or payer not in paid:
            continue
        sharing = set(expense.shared_with)
        paid[payer] += amount
        for who, part in split(amount, [who for who in order if who in sharing] or [payer]).items():
            share[who] += part
    return paid, share


def _total(converted: Converted, kind: ExpenseKind) -> int:
    return sum(amount for expense, amount in converted if expense.kind is kind)


def _unconverted(expense: TripExpense, reason: NoRate) -> TravelUnconvertedOut:
    return TravelUnconvertedOut(
        expense_id=str(expense.id),
        title=expense.title,
        kind=expense.kind,
        category=expense.category,
        amount_minor=expense.amount_minor,
        currency=expense.currency,
        reason=reason,
    )


class Budget:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.trips = Trips(ctx)
        self.expenses = ExpenseRepository(ctx.scope, ctx.clock)
        self._found: dict[tuple[str, dt.date], FxRate | NoRate] = {}
        self._why: dict[str, NoRate] = {}

    async def _no_rate(self, currency: str) -> NoRate:
        """Why a rate from or into ``currency`` can't be had just now: none ever will, when the
        provider lacks it; else it is only unavailable. Once per load."""
        if currency not in self._why:
            self._why[currency] = "unsupported" if await _unsupported(currency) else "unavailable"
        return self._why[currency]

    async def _rates(self, base: str, day: dt.date) -> FxRate | NoRate:
        """``base``'s rates of that very day, or why there are none: once per load."""
        key = (base, day)
        if key not in self._found:
            self._found[key] = await self._look_up(base, day)
        return self._found[key]

    async def _look_up(self, base: str, day: dt.date) -> FxRate | NoRate:
        if PROVIDER_REST.on(self.ctx.clock.now()):
            kept = await FxRate.find_one({"base": base, "day": day.isoformat()})
            return kept or await self._no_rate(base)
        try:
            found: FxRate | None = await Fx(self.ctx).rates(base, day)
        except FxUnsupportedError:
            PROVIDER_REST.unsupported.add(base)
            return "unsupported"
        except FxUnavailableError:
            found = None
        now = self.ctx.clock.now()
        if found is None or found.day != day.isoformat():  # none, or an older day's: it failed
            PROVIDER_REST.begin(now)
            return await self._no_rate(base)
        if self._not_refreshed(found, now):  # still that day's own, so it counts
            PROVIDER_REST.begin(now)
        return found

    def _not_refreshed(self, found: FxRate, now: dt.datetime) -> bool:
        """Today's rates as kept from before the cache's time: ``Fx`` asked again for them, and
        its provider failed. A day gone by is fetched once, so its rates are never old."""
        if found.day != self.ctx.today().isoformat():
            return False
        fresh_for = dt.timedelta(hours=self.ctx.settings.fx.cache_ttl_hours)
        return _aware(found.fetched_at) <= now - fresh_for

    async def _converted(
        self, trip: Trip, expenses: Iterable[TripExpense]
    ) -> tuple[Converted, list[TravelUnconvertedOut]]:
        converted: Converted = []
        unconverted: list[TravelUnconvertedOut] = []
        today = self.ctx.today()
        for expense in expenses:
            if expense.currency == trip.currency:
                converted.append((expense, expense.amount_minor))
                continue
            day = min(dt.date.fromisoformat(expense.day) if expense.day else today, today)
            found = await self._rates(expense.currency, day)
            if found == "unavailable":  # and never will be, when the provider lacks the trip's
                found = await self._no_rate(trip.currency)
            if isinstance(found, str):
                unconverted.append(_unconverted(expense, found))
            elif trip.currency not in found.rates:
                unconverted.append(_unconverted(expense, "unsupported"))
            else:
                rate = Decimal(found.rates[trip.currency])
                amount = convert(expense.amount_minor, expense.currency, trip.currency, rate)
                converted.append((expense, amount))
        return converted, unconverted

    async def summary(self, trip_id: str) -> TravelBudgetOut:
        trip = await self.trips.get(trip_id)
        converted, unconverted = await self._converted(trip, await self.expenses.of_trip(trip.id))
        paid, share = _shared(trip, converted)
        balances = {who: paid[who] - share[who] for who in paid}
        actual = _total(converted, ExpenseKind.ACTUAL)
        return TravelBudgetOut(
            currency=trip.currency,
            budget_minor=trip.budget_minor,
            estimated_minor=_total(converted, ExpenseKind.ESTIMATE),
            actual_minor=actual,
            remaining_minor=None if trip.budget_minor is None else trip.budget_minor - actual,
            categories=_categories(converted),
            unconverted=unconverted,
            balances=[
                TravelBalanceOut(
                    traveller_id=who,
                    paid_minor=paid[who],
                    share_minor=share[who],
                    balance_minor=balances[who],
                )
                for who in paid
            ],
            settlements=settle(balances, list(paid)),
        )
