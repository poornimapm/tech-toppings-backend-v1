"""The user's figures for a span of days, from the same filters as the entries list: a chart
that opens the list with its own filters always finds the total it showed."""

from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from functools import cache
from pathlib import Path
from statistics import fmean, median
from typing import Any

import yaml
from pydantic import BaseModel

from app.modules.expenses.domain import EntryKind
from app.modules.expenses.features.analytics.schemas import (
    AnomaliesOut,
    ByCategoryOut,
    ByPersonOut,
    CategoryShare,
    CumulativeOut,
    DailyOut,
    DayAmount,
    ForecastOut,
    HeatDay,
    HeatmapOut,
    HeatWeek,
    MerchantShare,
    Month,
    MonthlyOut,
    NetOut,
    PersonShare,
    RunningTotal,
    Span,
    SummaryOut,
    TopMerchantsOut,
    TopProduct,
    TopProductsOut,
    Totals,
    Unusual,
    WeekdayAmount,
)
from app.modules.expenses.features.entries.models import Entry, EntryRepository, sums
from app.modules.expenses.features.entries.schemas import EntryFilters, on_days
from app.modules.expenses.features.products.service import Products
from app.platform import ActorContext

DATA = Path(__file__).parents[2] / "data" / "analytics.yaml"
WEEK = 7
PERCENT = 100
MAD_SCALE = 1.4826  # makes the median absolute deviation comparable to a standard deviation


class _Top(BaseModel):
    limit: int


class _Monthly(BaseModel):
    months: int


class _Forecast(BaseModel):
    lookback_days: int
    band_percentiles: tuple[float, float]


class _Anomalies(BaseModel):
    lookback_days: int
    min_history: int
    threshold: float
    min_ratio: float


class Tuning(BaseModel):
    top: _Top
    monthly: _Monthly
    forecast: _Forecast
    anomalies: _Anomalies


@cache
def tuning() -> Tuning:
    return Tuning.model_validate(yaml.safe_load(DATA.read_text(encoding="utf-8")))


def change_pct(now: int, before: int) -> float | None:
    return round((now - before) / before * PERCENT, 1) if before else None


def _share(amount: int, total: int) -> float:
    return round(amount / total * PERCENT, 1) if total else 0.0


def _days(start: dt.date, end: dt.date) -> list[dt.date]:
    return [start + dt.timedelta(days=n) for n in range((end - start).days + 1)]


def _month_end(day: dt.date) -> dt.date:
    following = (day.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return following - dt.timedelta(days=1)


def _percentile(values: Sequence[float], pct: float) -> float:
    """Linear interpolation between the nearest ranks, as spreadsheets do."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    rank = (len(ordered) - 1) * pct / PERCENT
    low, high = math.floor(rank), math.ceil(rank)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _score(amount: float, usual: float, spread: float) -> float:
    """How far above usual, in robust deviations. Always the same amount before (no spread):
    any rise is far from usual."""
    if spread:
        return (amount - usual) / spread
    return math.inf if amount > usual else 0.0


def _totals(rows: Iterable[Mapping[str, Any]]) -> Totals:
    found = Totals()
    for row in rows:
        found.count += row["count"]
        if row["kind"] == EntryKind.EXPENSE:
            found.spent_minor += row["amount_minor"]
        else:
            found.received_minor += row["amount_minor"]
    found.net_minor = found.received_minor - found.spent_minor
    return found


def _savings_rate(totals: Totals) -> float | None:
    return _share(totals.net_minor, totals.received_minor) if totals.received_minor else None


class Analytics:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.entries = EntryRepository(ctx.scope, ctx.clock)
        self.currency = ctx.user.base_currency

    # ------------------------------------------------------------------ helpers

    def today(self) -> dt.date:
        return self.ctx.today()

    def span(self, filters: EntryFilters) -> Span:
        """The days asked for; this month so far when none are."""
        end = filters.end or (
            self.today() if filters.start is None else max(filters.start, self.today())
        )
        return Span(start=filters.start or end.replace(day=1), end=end)

    @staticmethod
    def previous(span: Span) -> Span:
        """What to compare with: the same days of the month before when the span starts a
        month (1-14 October against 1-14 September), else the same number of days just
        before (the last 90 days against the 90 before them)."""
        length = span.end - span.start
        if span.start.day == 1:
            start = (span.start - dt.timedelta(days=1)).replace(day=1)
            return Span(start=start, end=min(start + length, _month_end(start)))
        end = span.start - dt.timedelta(days=1)
        return Span(start=end - length, end=end)

    def criteria(
        self,
        filters: EntryFilters,
        span: Span,
        kind: EntryKind | None = None,
        currency: str | None = None,
    ) -> dict[str, Any]:
        """Entries in the base currency unless ``currency`` says which."""
        found = filters.criteria() | on_days(span.start, span.end)
        found["currency"] = currency or self.currency
        if kind is not None:
            found["kind"] = kind
        return self.entries.query(found, include_deleted=filters.deleted)

    async def totals(
        self, span: Span, filters: EntryFilters | None = None, currency: str | None = None
    ) -> Totals:
        criteria = self.criteria(filters or EntryFilters(), span, currency=currency)
        return _totals(await sums(criteria, "kind"))

    async def _by_day(self, filters: EntryFilters, span: Span, kind: EntryKind) -> dict[str, int]:
        rows = await sums(self.criteria(filters, span, kind), "date")
        return {row["date"]: row["amount_minor"] for row in rows}

    # ------------------------------------------------------------------ figures

    async def summary(self, filters: EntryFilters) -> SummaryOut:
        span, before = self.span(filters), self.previous(self.span(filters))
        now, earlier = await self.totals(span, filters), await self.totals(before, filters)
        days = (span.end - span.start).days + 1
        return SummaryOut(
            **span.model_dump(),
            **now.model_dump(),
            currency=self.currency,
            days=days,
            avg_daily_spent_minor=round(now.spent_minor / days),
            savings_rate_pct=_savings_rate(now),
            previous=before,
            previous_totals=earlier,
            spent_change_pct=change_pct(now.spent_minor, earlier.spent_minor),
            received_change_pct=change_pct(now.received_minor, earlier.received_minor),
        )

    async def _shares(
        self, filters: EntryFilters, field: str
    ) -> tuple[Span, EntryKind, int, list[dict[str, Any]]]:
        span, kind = self.span(filters), filters.kind or EntryKind.EXPENSE
        rows = await sums(self.criteria(filters, span, kind), field)
        total = sum(row["amount_minor"] for row in rows)
        rows.sort(key=lambda row: (-row["amount_minor"], str(row.get(field))))
        return span, kind, total, rows

    async def by_category(self, filters: EntryFilters) -> ByCategoryOut:
        span, kind, total, rows = await self._shares(filters, "category_id")
        items = [
            CategoryShare(
                category_id=row["category_id"],
                amount_minor=row["amount_minor"],
                count=row["count"],
                share_pct=_share(row["amount_minor"], total),
            )
            for row in rows
        ]
        return ByCategoryOut(
            **span.model_dump(), currency=self.currency, kind=kind, total_minor=total, items=items
        )

    async def by_person(self, filters: EntryFilters) -> ByPersonOut:
        span, kind, total, rows = await self._shares(filters, "for_whom")
        items = [
            PersonShare(
                name=row.get("for_whom"),
                amount_minor=row["amount_minor"],
                count=row["count"],
                share_pct=_share(row["amount_minor"], total),
            )
            for row in rows
        ]
        return ByPersonOut(
            **span.model_dump(), currency=self.currency, kind=kind, total_minor=total, items=items
        )

    async def top_merchants(self, filters: EntryFilters) -> TopMerchantsOut:
        span, kind, total, rows = await self._shares(filters, "merchant")
        items = [
            MerchantShare(
                merchant=row["merchant"],
                amount_minor=row["amount_minor"],
                count=row["count"],
                share_pct=_share(row["amount_minor"], total),
            )
            for row in rows
            if row.get("merchant")
        ]
        return TopMerchantsOut(
            **span.model_dump(),
            currency=self.currency,
            kind=kind,
            total_minor=total,
            items=items[: tuning().top.limit],
        )

    async def daily(self, filters: EntryFilters) -> DailyOut:
        span, kind = self.span(filters), filters.kind or EntryKind.EXPENSE
        rows = {row["date"]: row for row in await sums(self.criteria(filters, span, kind), "date")}
        points = [
            DayAmount(
                date=day,
                amount_minor=rows.get(day.isoformat(), {}).get("amount_minor", 0),
                count=rows.get(day.isoformat(), {}).get("count", 0),
            )
            for day in _days(span.start, span.end)
        ]
        return DailyOut(**span.model_dump(), currency=self.currency, kind=kind, points=points)

    def _month_span(self, filters: EntryFilters) -> Span:
        """The range asked for, else the last months (tuning), this one included."""
        if filters.start or filters.end:
            return self.span(filters)
        end = self.today()
        start = end.replace(day=1)
        for _ in range(tuning().monthly.months - 1):
            start = (start - dt.timedelta(days=1)).replace(day=1)
        return Span(start=start, end=end)

    async def monthly(self, filters: EntryFilters) -> MonthlyOut:
        span = self._month_span(filters)
        by_month: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in await sums(self.criteria(filters, span), "date", "kind"):
            by_month[row["date"][:7]].append(row)
        months, running, day = [], 0, span.start
        while day <= span.end:
            last = min(_month_end(day), span.end)
            totals = _totals(by_month.get(day.isoformat()[:7], []))
            running += totals.net_minor
            months.append(
                Month(
                    **totals.model_dump(),
                    month=day.isoformat()[:7],
                    start=day,
                    end=last,
                    savings_rate_pct=_savings_rate(totals),
                    cumulative_net_minor=running,
                )
            )
            day = last + dt.timedelta(days=1)
        return MonthlyOut(**span.model_dump(), currency=self.currency, months=months)

    async def net(self, filters: EntryFilters) -> NetOut:
        """Money in, money out and what was kept, month by month (U1)."""
        months = await self.monthly(filters)
        total = Totals(
            spent_minor=sum(month.spent_minor for month in months.months),
            received_minor=sum(month.received_minor for month in months.months),
            count=sum(month.count for month in months.months),
        )
        total.net_minor = total.received_minor - total.spent_minor
        return NetOut(**months.model_dump(), total=total, savings_rate_pct=_savings_rate(total))

    async def top_products(self, filters: EntryFilters) -> TopProductsOut:
        span = self.span(filters)
        rows = await Products(self.ctx).spending(span.start, span.end, tuning().top.limit)
        return TopProductsOut(
            **span.model_dump(),
            currency=self.currency,
            items=[TopProduct.model_validate(row) for row in rows],
        )

    async def heatmap(self, filters: EntryFilters) -> HeatmapOut:
        """Each day of the span by week (Monday first), and the average of each weekday."""
        span, kind = self.span(filters), filters.kind or EntryKind.EXPENSE
        spent = await self._by_day(filters, span, kind)
        monday = span.start - dt.timedelta(days=span.start.weekday())
        weeks = []
        while monday <= span.end:
            days = [monday + dt.timedelta(days=n) for n in range(WEEK)]
            weeks.append(
                HeatWeek(
                    start=monday,
                    days=[
                        HeatDay(date=day, amount_minor=spent.get(day.isoformat(), 0))
                        if span.start <= day <= span.end
                        else None
                        for day in days
                    ],
                )
            )
            monday += dt.timedelta(days=WEEK)
        weekdays = []
        for weekday in range(WEEK):
            amounts = [
                spent.get(day.isoformat(), 0)
                for day in _days(span.start, span.end)
                if day.weekday() == weekday
            ]
            weekdays.append(
                WeekdayAmount(
                    weekday=weekday,
                    total_minor=sum(amounts),
                    average_minor=round(fmean(amounts)) if amounts else 0,
                )
            )
        return HeatmapOut(
            **span.model_dump(), currency=self.currency, kind=kind, weeks=weeks, weekdays=weekdays
        )

    async def cumulative(self, filters: EntryFilters) -> CumulativeOut:
        span, kind = self.span(filters), filters.kind or EntryKind.EXPENSE
        before = self.previous(span)

        async def running(days: Span) -> list[RunningTotal]:
            spent, total, found = await self._by_day(filters, days, kind), 0, []
            for day in _days(days.start, days.end):
                total += spent.get(day.isoformat(), 0)
                found.append(RunningTotal(date=day, total_minor=total))
            return found

        return CumulativeOut(
            **span.model_dump(),
            currency=self.currency,
            kind=kind,
            points=await running(span),
            previous=before,
            previous_points=await running(before),
        )

    async def forecast(self, day: dt.date | None = None) -> ForecastOut:
        """The month of ``day`` (default today): spent so far, and where it is heading."""
        rules, today = tuning().forecast, self.today()
        start = (day or today).replace(day=1)
        end = _month_end(start)
        upto = min(end, today)
        everything = EntryFilters()
        spent = sum(
            (
                await self._by_day(everything, Span(start=start, end=upto), EntryKind.EXPENSE)
            ).values()
        )
        days_left = (end - upto).days if upto >= start else (end - start).days + 1
        first = await self._first_expense()
        window = Span(
            start=max(upto - dt.timedelta(days=rules.lookback_days), first or upto),
            end=upto - dt.timedelta(days=1),
        )
        daily: list[float] = []
        if window.end >= window.start:
            by_day = await self._by_day(everything, window, EntryKind.EXPENSE)
            daily = [by_day.get(d.isoformat(), 0) for d in _days(window.start, window.end)]
        pace = fmean(daily) if daily else 0.0
        # The band reads whole weeks (their average day): spending a few days a week would
        # make most single days zero, and the band would say nothing.
        weekly = [fmean(daily[at : at + WEEK]) for at in range(0, len(daily) - WEEK + 1, WEEK)]
        low, high = (_percentile(weekly or daily, pct) for pct in rules.band_percentiles)
        previous_start = (start - dt.timedelta(days=1)).replace(day=1)
        previous = await self._by_day(
            everything,
            Span(start=previous_start, end=start - dt.timedelta(days=1)),
            EntryKind.EXPENSE,
        )
        return ForecastOut(
            start=start,
            end=end,
            currency=self.currency,
            month=start.isoformat()[:7],
            today=today,
            spent_minor=spent,
            days_left=days_left,
            pace_daily_minor=round(pace),
            projected_minor=spent + round(pace * days_left),
            low_minor=spent + round(low * days_left),
            high_minor=spent + round(high * days_left),
            lookback_days=len(daily),
            previous_month_minor=sum(previous.values()),
        )

    async def _first_expense(self) -> dt.date | None:
        row = await Entry.get_pymongo_collection().find_one(
            self.entries.query({"kind": EntryKind.EXPENSE, "currency": self.currency}),
            {"date": 1},
            sort=[("date", 1)],
        )
        return dt.date.fromisoformat(row["date"]) if row else None

    async def anomalies(self, filters: EntryFilters) -> AnomaliesOut:
        """Expenses far above what the user usually spends in their category (tuning)."""
        rules, span = tuning().anomalies, self.span(filters)
        history = Span(start=span.start - dt.timedelta(days=rules.lookback_days), end=span.end)
        rows = (
            await Entry.get_pymongo_collection()
            .find(
                self.criteria(filters, history, EntryKind.EXPENSE),
                {"title": 1, "date": 1, "category_id": 1, "amount_minor": 1},
            )
            .to_list()
        )
        by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_category[row["category_id"]].append(row)
        found: list[tuple[float, Unusual]] = []
        for row in rows:
            day = dt.date.fromisoformat(row["date"])
            if day < span.start:
                continue
            since = (day - dt.timedelta(days=rules.lookback_days)).isoformat()
            earlier = [
                other["amount_minor"]
                for other in by_category[row["category_id"]]
                if since <= other["date"] < row["date"]
            ]
            if len(earlier) < rules.min_history:
                continue
            usual = median(earlier)
            spread = MAD_SCALE * median(abs(amount - usual) for amount in earlier)
            amount = row["amount_minor"]
            score = _score(amount, usual, spread)
            if score >= rules.threshold and amount >= rules.min_ratio * usual:
                found.append(
                    (
                        score,
                        Unusual(
                            entry_id=str(row["_id"]),
                            title=row["title"],
                            date=day,
                            category_id=row["category_id"],
                            amount_minor=amount,
                            usual_minor=round(usual),
                            times_usual=round(amount / usual, 1) if usual else 0.0,
                        ),
                    )
                )
        found.sort(key=lambda pair: (-pair[0], -pair[1].amount_minor))
        return AnomaliesOut(
            **span.model_dump(),
            currency=self.currency,
            items=[item for _, item in found[: tuning().top.limit]],
        )
