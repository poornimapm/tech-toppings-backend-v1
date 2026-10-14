"""``/v1/expenses/analytics``: figures for a span of days. Each takes the entries list's own
filters (``from``, ``to``, ``kind``, ``category_id``, ``for_whom``, ``merchant`` ...), so the
list opened from a chart shows the same total."""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.modules.expenses.features.analytics.schemas import (
    AnomaliesOut,
    ByCategoryOut,
    ByPersonOut,
    CumulativeOut,
    DailyOut,
    ForecastOut,
    HeatmapOut,
    MonthlyOut,
    NetOut,
    SummaryOut,
    TopMerchantsOut,
    TopProductsOut,
)
from app.modules.expenses.features.analytics.service import Analytics
from app.modules.expenses.features.entries.schemas import EntryFilters
from app.platform import ActorDep, error_responses

router = APIRouter(prefix="/analytics")
ERRORS = error_responses(401, 403)


def analytics(ctx: ActorDep) -> Analytics:
    return Analytics(ctx)


AnalyticsDep = Annotated[Analytics, Depends(analytics)]
Filters = Annotated[EntryFilters, Query()]


@router.get(
    "/summary", summary="Spent, received and kept, against the days before", responses=ERRORS
)
async def summary(service: AnalyticsDep, filters: Filters) -> SummaryOut:
    return await service.summary(filters)


@router.get("/by-category", summary="Totals by category (default: spending)", responses=ERRORS)
async def by_category(service: AnalyticsDep, filters: Filters) -> ByCategoryOut:
    return await service.by_category(filters)


@router.get("/by-person", summary="Totals by who it was for (U6)", responses=ERRORS)
async def by_person(service: AnalyticsDep, filters: Filters) -> ByPersonOut:
    return await service.by_person(filters)


@router.get("/daily", summary="A total for every day", responses=ERRORS)
async def daily(service: AnalyticsDep, filters: Filters) -> DailyOut:
    return await service.daily(filters)


@router.get("/monthly", summary="Spent and received, month by month", responses=ERRORS)
async def monthly(service: AnalyticsDep, filters: Filters) -> MonthlyOut:
    return await service.monthly(filters)


@router.get("/net", summary="Net savings, month by month and in all (U1)", responses=ERRORS)
async def net(service: AnalyticsDep, filters: Filters) -> NetOut:
    return await service.net(filters)


@router.get("/top-products", summary="What was spent on each product", responses=ERRORS)
async def top_products(service: AnalyticsDep, filters: Filters) -> TopProductsOut:
    return await service.top_products(filters)


@router.get("/top-merchants", summary="Where the money went", responses=ERRORS)
async def top_merchants(service: AnalyticsDep, filters: Filters) -> TopMerchantsOut:
    return await service.top_merchants(filters)


@router.get("/weekday-heatmap", summary="Each day by week, and each weekday", responses=ERRORS)
async def weekday_heatmap(service: AnalyticsDep, filters: Filters) -> HeatmapOut:
    return await service.heatmap(filters)


@router.get("/cumulative", summary="The running total, against the days before", responses=ERRORS)
async def cumulative(service: AnalyticsDep, filters: Filters) -> CumulativeOut:
    return await service.cumulative(filters)


@router.get("/forecast", summary="Where this month's spending is heading", responses=ERRORS)
async def forecast(
    service: AnalyticsDep,
    day: Annotated[dt.date | None, Query(alias="date", description="A day of the month")] = None,
) -> ForecastOut:
    return await service.forecast(day)


@router.get(
    "/anomalies", summary="Expenses far above the usual for their category", responses=ERRORS
)
async def anomalies(service: AnalyticsDep, filters: Filters) -> AnomaliesOut:
    return await service.anomalies(filters)
