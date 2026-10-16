"""An ExpenseQuery compiled into a MongoDB pipeline, from a whitelist: each metric, grouping and
order maps to a fixed expression, and the user's words are only ever values (an exact match or
an escaped pattern), never field names or operators. The pipeline always starts by keeping the
caller's own live entries counted in their currency, so no query can read anyone else's.

Stages are only ``$match``, ``$unwind`` (tags), ``$group``, ``$sort`` and ``$limit``; the limit
is the query's, which its schema caps (tests/modules/expenses/test_query_compiler.py)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from app.modules.expenses.features.entries.models import BASE_AMOUNT, in_base
from app.modules.expenses.features.queries.schemas import ExpenseQuery, GroupBy, Metric, SortOrder

KEYS: dict[GroupBy, Any] = {
    GroupBy.NONE: None,
    GroupBy.CATEGORY: "$category_id",
    GroupBy.MERCHANT: "$merchant",
    GroupBy.PERSON: "$for_whom",
    GroupBy.TAG: "$tags",
    GroupBy.DAY: "$date",
    GroupBy.MONTH: {"$substrBytes": ["$date", 0, 7]},  # YYYY-MM of a YYYY-MM-DD day
    GroupBy.KIND: "$kind",
    GroupBy.PAYMENT_MODE: "$payment_mode",
}
# Money is counted in the base currency (each entry's amount on its day, ADR-0004).
VALUES: dict[Metric, dict[str, Any]] = {
    Metric.TOTAL: {"$sum": BASE_AMOUNT},
    Metric.COUNT: {"$sum": 1},
    Metric.AVERAGE: {"$avg": BASE_AMOUNT},
    Metric.LARGEST: {"$max": BASE_AMOUNT},
}
ORDERS: dict[SortOrder, dict[str, int]] = {
    SortOrder.VALUE_DESC: {"value": -1, "_id": 1},
    SortOrder.VALUE_ASC: {"value": 1, "_id": 1},
    SortOrder.KEY: {"_id": 1},
}
STAGES = frozenset({"$match", "$unwind", "$group", "$sort", "$limit"})


def _exactly(text: str) -> dict[str, str]:
    return {"$regex": f"^{re.escape(text)}$", "$options": "i"}


def _filters(query: ExpenseQuery) -> dict[str, Any]:
    found: dict[str, Any] = {}
    if query.kind is not None:
        found["kind"] = query.kind.value
    days = {op: day.isoformat() for op, day in (("$gte", query.start), ("$lte", query.end)) if day}
    if days:
        found["date"] = days
    if query.category_ids:
        found["category_id"] = {"$in": list(query.category_ids)}
    if query.merchant:
        found["merchant"] = _exactly(query.merchant)
    if query.person:
        found["for_whom"] = _exactly(query.person)
    if query.tag:
        found["tags"] = query.tag.casefold()
    amounts = {
        op: amount
        for op, amount in (("$gte", query.min_amount_minor), ("$lte", query.max_amount_minor))
        if amount is not None
    }
    if amounts:
        found["amount_minor"] = amounts  # as paid
    if query.text:
        pattern = {"$regex": re.escape(query.text), "$options": "i"}
        found["$or"] = [{name: pattern} for name in ("title", "merchant", "note")]
    return found


def compile_query(
    query: ExpenseQuery, own: Mapping[str, Any], currency: str
) -> list[dict[str, Any]]:
    """The pipeline for ``query``. ``own`` keeps the caller's own live entries (their
    repository's query); it is applied last, so nothing in the query can widen it."""
    filters = _filters(query)
    match = {**filters, **in_base(currency), **own}
    stages: list[dict[str, Any]] = [{"$match": match}]
    if query.group_by is GroupBy.TAG:
        stages.append({"$unwind": "$tags"})
        if query.tag:  # each entry counts under the tag asked for, not its others
            stages.append({"$match": {"tags": query.tag.casefold()}})
    stages += [
        {
            "$group": {
                "_id": KEYS[query.group_by],
                "value": VALUES[query.metric],
                "count": {"$sum": 1},
            }
        },
        {"$sort": ORDERS[query.sort]},
        {"$limit": query.limit},
    ]
    return stages
