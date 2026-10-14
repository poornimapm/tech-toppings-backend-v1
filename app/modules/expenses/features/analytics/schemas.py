"""What the analytics endpoints answer. Money is in minor units of ``currency`` (the user's base
currency); a ``*_pct`` is ``None`` when there is nothing to compare with."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field

from app.modules.expenses.domain import EntryKind


class Span(BaseModel):
    start: dt.date
    end: dt.date


class Totals(BaseModel):
    spent_minor: int = 0
    received_minor: int = 0
    net_minor: int = 0
    count: int = 0


class SummaryOut(Span, Totals):
    currency: str
    days: int
    avg_daily_spent_minor: int
    savings_rate_pct: float | None  # what was kept of what came in
    previous: Span
    previous_totals: Totals
    spent_change_pct: float | None
    received_change_pct: float | None


class Share(BaseModel):
    amount_minor: int
    count: int
    share_pct: float


class CategoryShare(Share):
    category_id: str


class PersonShare(Share):
    name: str | None  # None: nobody named (usually the user themself)


class MerchantShare(Share):
    merchant: str


class SharesOut(Span):
    currency: str
    kind: EntryKind
    total_minor: int


class ByCategoryOut(SharesOut):
    items: list[CategoryShare]


class ByPersonOut(SharesOut):
    items: list[PersonShare]


class TopMerchantsOut(SharesOut):
    items: list[MerchantShare]


class DayAmount(BaseModel):
    date: dt.date
    amount_minor: int
    count: int = 0


class DailyOut(Span):
    currency: str
    kind: EntryKind
    points: list[DayAmount]


class Month(Totals):
    month: str  # YYYY-MM
    start: dt.date
    end: dt.date  # clipped to the range asked for
    savings_rate_pct: float | None = None
    cumulative_net_minor: int = 0


class MonthlyOut(Span):
    currency: str
    months: list[Month]


class NetOut(MonthlyOut):
    total: Totals
    savings_rate_pct: float | None


class TopProduct(BaseModel):
    product_id: str
    name: str
    amount_minor: int
    count: int
    price_unit: str
    last_unit_price_minor: int | None


class TopProductsOut(Span):
    currency: str
    items: list[TopProduct]


class HeatDay(BaseModel):
    date: dt.date
    amount_minor: int


class HeatWeek(BaseModel):
    start: dt.date  # a Monday
    days: list[HeatDay | None]  # Monday first; None outside the range


class WeekdayAmount(BaseModel):
    weekday: int = Field(ge=0, le=6, description="0 = Monday")
    total_minor: int
    average_minor: int  # per such day in the range


class HeatmapOut(Span):
    currency: str
    kind: EntryKind
    weeks: list[HeatWeek]
    weekdays: list[WeekdayAmount]


class RunningTotal(BaseModel):
    date: dt.date
    total_minor: int


class CumulativeOut(Span):
    currency: str
    kind: EntryKind
    points: list[RunningTotal]
    previous: Span
    previous_points: list[RunningTotal]  # the same day of the period before, for pace


class ForecastOut(Span):
    currency: str
    month: str
    today: dt.date
    spent_minor: int  # so far
    days_left: int
    pace_daily_minor: int
    projected_minor: int
    low_minor: int
    high_minor: int
    lookback_days: int  # how many days the pace was read from
    previous_month_minor: int


class Unusual(BaseModel):
    entry_id: str
    title: str
    date: dt.date
    category_id: str
    amount_minor: int
    usual_minor: int  # the category's median before it
    times_usual: float


class AnomaliesOut(Span):
    currency: str
    items: list[Unusual]
