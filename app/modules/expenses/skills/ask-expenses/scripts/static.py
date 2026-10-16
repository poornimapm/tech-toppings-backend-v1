"""Static mode for ask-expenses: a question read with the skill's words into a query for
``expenses_query``, and its figures said with the skill's templates (the module's manifest
words each code). Without AI it reads the metric, kind, grouping, period and category names;
anything subtler needs AI."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any

from app.platform import (
    StaticOutcome,
    StaticRequest,
    ToolBox,
    load_lexicon,
    match_action,
    normalise,
    to_minor,
)

GROUPS = {"category": "category", "merchant": "merchant", "person": "person"}
WEEKDAYS = 7


def _found(text: str, section: Mapping[str, Any]) -> str | None:
    """The first of the section's names whose words the question has."""
    found = match_action(text, {"actions": section})
    return found[0] if found else None


def days(period: str, today: dt.date) -> tuple[dt.date, dt.date]:
    """The first and last day a period word means, today being ``today``."""
    month_start = today.replace(day=1)
    week_start = today - dt.timedelta(days=today.weekday())
    last_month_end = month_start - dt.timedelta(days=1)
    spans = {
        "today": (today, today),
        "yesterday": (today - dt.timedelta(days=1),) * 2,
        "this_week": (week_start, today),
        "last_week": (
            week_start - dt.timedelta(days=WEEKDAYS),
            week_start - dt.timedelta(days=1),
        ),
        "this_month": (month_start, today),
        "last_month": (last_month_end.replace(day=1), last_month_end),
        "this_year": (today.replace(month=1, day=1), today),
    }
    return spans[period]


async def _categories(text: str, kind: str, tools: ToolBox) -> list[str]:
    """Ids of the user's categories of this kind that the question names."""
    found = []
    for item in await tools.read("expenses_categories", {}):
        names = {normalise(item["name"]), normalise(item["id"].replace("_", " "))}
        if item["kind"] == kind and any(name and name in text for name in names):
            found.append(item["id"])
    return found


async def run(request: StaticRequest, tools: ToolBox) -> StaticOutcome | None:
    lexicon = load_lexicon(request.references)
    text = normalise(request.input)
    if not request.chosen and _found(text, lexicon["question"]) is None:
        return None
    metric = _found(text, lexicon["metric"]) or "total"
    kind = _found(text, lexicon["kind"]) or "expense"
    group = GROUPS.get(_found(text, lexicon["group"]) or "", "none")
    start, end = days(_found(text, lexicon["period"]) or "this_month", request.today)
    top = int(lexicon["answer"]["top_rows"])
    query = {
        "metric": "total" if group != "none" else metric,
        "kind": kind,
        "group_by": group,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "category_ids": await _categories(text, kind, tools),
        "limit": top if group != "none" else 1,
    }
    result = await tools.read("expenses_query", {"query": query})
    currency, rows = result["currency"], result["rows"]
    params: dict[str, Any] = {
        "currency": currency,
        "from_on": start.isoformat(),
        "to_on": end.isoformat(),
    }
    if not rows or not rows[0]["count"]:
        return StaticOutcome("ask-expenses.none", params)
    if group == "none":
        row = rows[0]
        amount = to_minor(row["value"], currency) if result["money"] else None
        figures = {"amount_minor": amount, "count": row["count"]}
        return StaticOutcome(f"ask-expenses.{metric}.{kind}", params | figures)
    places = ("first", "second", "third")[: len(rows)]
    for place, row in zip(places, rows, strict=False):
        params[f"{place}_name"] = row["key"] or "-"
        params[f"{place}_minor"] = to_minor(row["value"], currency)
    return StaticOutcome(f"ask-expenses.top.{len(places)}", params)
