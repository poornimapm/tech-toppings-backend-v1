from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.modules.expenses.features.insights.schemas import InsightOut, InsightQuery
from app.modules.expenses.features.insights.service import Insights
from app.platform import ActorDep, error_responses

router = APIRouter(prefix="/insights")


@router.get(
    "",
    summary="A week or month in figures, and a summary of them (kept while they don't change)",
    responses=error_responses(401, 403),
)
async def read_insight(ctx: ActorDep, query: Annotated[InsightQuery, Query()]) -> InsightOut:
    return await Insights(ctx).explain(query.period, query.day)


@router.post(
    "/refresh",
    summary="Write the summary again, even if the figures haven't changed",
    responses=error_responses(401, 403),
)
async def refresh_insight(ctx: ActorDep, query: Annotated[InsightQuery, Query()]) -> InsightOut:
    return await Insights(ctx).explain(query.period, query.day, refresh=True)
