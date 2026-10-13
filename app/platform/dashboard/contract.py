"""The widget contract.

A widget is an async function ``(ctx: ActorContext, period: DateRange)`` in a package's
``widgets.py``, decorated with ``@widget("kpi" | "series" | "list")`` and returning the
matching shape below. It reads through ``ctx.scope``, so the same function serves a user's
own dashboard and, with an unrestricted scope, the admin's "all users" view.

Titles are not here: the client names a widget from its key, in the user's language.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated, Any, Literal, TypeVar

from beanie import Document
from pydantic import BaseModel, Field

from app.core.daterange import DateRange
from app.platform.context import ActorContext

WIDGET_ATTRIBUTE = "__dashboard_widget__"
WidgetKind = Literal["kpi", "series", "list"]
ValueFormat = Literal["number", "currency"]


class Kpi(BaseModel):
    """One figure for the period, with the previous period's for a trend."""

    kind: Literal["kpi"] = "kpi"
    value: float
    previous: float | None = None
    format: ValueFormat = "number"
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")


class Point(BaseModel):
    day: date
    value: float


class Series(BaseModel):
    """One value per day of the period."""

    kind: Literal["series"] = "series"
    points: list[Point]
    format: ValueFormat = "number"
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")


class Item(BaseModel):
    """A row: either the user's own text (``title``) or a message ``code`` + ``params`` the
    client translates."""

    title: str | None = None
    code: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    at: datetime | None = None


class Items(BaseModel):
    kind: Literal["list"] = "list"
    items: list[Item]


WidgetData = Annotated[Kpi | Series | Items, Field(discriminator="kind")]
WidgetProvider = Callable[[ActorContext, DateRange], Awaitable[Kpi | Series | Items]]
F = TypeVar("F", bound=WidgetProvider)


@dataclass(frozen=True)
class WidgetSpec:
    name: str
    kind: WidgetKind
    fn: WidgetProvider


def widget(kind: WidgetKind) -> Callable[[F], F]:
    def register(fn: F) -> F:
        target: Callable[..., Any] = fn
        if not inspect.iscoroutinefunction(target):
            raise TypeError(f"widget {fn.__name__} must be async")
        setattr(fn, WIDGET_ATTRIBUTE, WidgetSpec(fn.__name__, kind, fn))
        return fn

    return register


# ------------------------------------------------------------------ helpers for widgets


async def count_in(
    model: type[Document], criteria: Mapping[str, Any], period: DateRange, field: str = "created_at"
) -> int:
    """Documents matching ``criteria`` (a repository ``query()``) dated within the period."""
    return await model.get_pymongo_collection().count_documents({**criteria, **period.match(field)})


async def daily_counts(
    model: type[Document], criteria: Mapping[str, Any], period: DateRange, field: str = "created_at"
) -> list[Point]:
    """Documents per day in the user's time zone; days with none are 0, so charts have no gaps."""
    day = {
        "$dateToString": {"format": "%Y-%m-%d", "date": f"${field}", "timezone": period.timezone}
    }
    cursor = await model.get_pymongo_collection().aggregate(
        [
            {"$match": {**criteria, **period.match(field)}},
            {"$group": {"_id": day, "n": {"$sum": 1}}},
        ]
    )
    counts = {row["_id"]: row["n"] async for row in cursor}
    return [Point(day=d, value=counts.get(d.isoformat(), 0)) for d in period.days]
