"""Answering an ExpenseQuery: compiled from the whitelist, run over the caller's entries."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from app.modules.expenses.features.entries.models import Entry, EntryRepository
from app.modules.expenses.features.queries.compiler import compile_query
from app.modules.expenses.features.queries.schemas import (
    ExpenseQuery,
    Metric,
    QueryResult,
    QueryRow,
)
from app.platform import ActorContext


def whole(value: float | int) -> int:
    """An average in minor units, rounded as people round money (halves away from zero)."""
    return int(Decimal(str(value)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


class Queries:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.entries = EntryRepository(ctx.scope, ctx.clock)

    async def run(self, query: ExpenseQuery) -> QueryResult:
        currency = self.ctx.user.base_currency
        pipeline = compile_query(query, self.entries.query(), currency)
        cursor = await Entry.get_pymongo_collection().aggregate(pipeline)
        rows = [
            QueryRow(
                key=None if row["_id"] is None else str(row["_id"]),
                value=whole(row["value"]),
                count=row["count"],
            )
            async for row in cursor
        ]
        return QueryResult(
            metric=query.metric,
            group_by=query.group_by,
            currency=currency,
            money=query.metric is not Metric.COUNT,
            rows=rows,
        )
