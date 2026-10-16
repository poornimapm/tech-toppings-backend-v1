from __future__ import annotations

from fastapi import APIRouter

from app.modules.expenses.features.queries.schemas import ExpenseQuery, QueryResult
from app.modules.expenses.features.queries.service import Queries
from app.platform import ActorDep, error_responses

router = APIRouter()


@router.post(
    "/query",
    summary="Answer a question about my entries: a metric, grouped and narrowed (read only)",
    responses=error_responses(401, 403, 422),
)
async def query_entries(body: ExpenseQuery, ctx: ActorDep) -> QueryResult:
    return await Queries(ctx).run(body)
