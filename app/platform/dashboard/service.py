from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from app.core.concurrency import settle
from app.core.daterange import DateRange, resolve_range
from app.platform.context import ActorContext
from app.platform.dashboard.contract import WIDGET_ATTRIBUTE, WidgetData, WidgetKind, WidgetSpec
from app.platform.plugins import PluginError, contributions


@dataclass(frozen=True)
class Widget:
    owner: str  # the contributing package's key: a module key, or a platform area
    spec: WidgetSpec


class WidgetView(BaseModel):
    key: str
    owner: str
    kind: WidgetKind
    data: WidgetData | None  # None: this widget couldn't load (the rest still did)


class DashboardOut(BaseModel):
    start: date
    end: date
    widgets: list[WidgetView]


def discover_widgets(packages: Iterable[str]) -> list[Widget]:
    widgets = [
        Widget(package.rsplit(".", 1)[-1], spec)
        for package in packages
        for spec in contributions(package, "widgets", WIDGET_ATTRIBUTE)
    ]
    names = [item.spec.name for item in widgets]
    if len(set(names)) != len(names):
        raise PluginError(
            f"duplicate widget names: {sorted(n for n in names if names.count(n) > 1)}"
        )
    return widgets


def period_for(ctx: ActorContext, start: date | None, end: date | None) -> DateRange:
    """``?from=&to=`` as whole days in the caller's time zone, within the configured limits."""
    settings = ctx.settings.dashboard
    return resolve_range(
        start,
        end,
        today=ctx.clock.now().astimezone(ZoneInfo(ctx.user.timezone)).date(),
        timezone=ctx.user.timezone,
        default_days=settings.default_range_days,
        max_days=settings.max_range_days,
    )


async def _load(ctx: ActorContext, period: DateRange, item: Widget) -> WidgetView:
    data = await settle(
        item.spec.fn(ctx, period),
        within=ctx.settings.dashboard.widget_timeout_seconds,
        event="widget_failed",
        widget=item.spec.name,
    )
    if data is not None and data.kind != item.spec.kind:  # the provider broke its own contract
        data = None
    return WidgetView(key=item.spec.name, owner=item.owner, kind=item.spec.kind, data=data)


async def build_dashboard(
    ctx: ActorContext, period: DateRange, widgets: Sequence[Widget]
) -> DashboardOut:
    """Every widget in parallel; a failing or slow one is reported as unavailable, not fatal."""
    views = await asyncio.gather(*(_load(ctx, period, item) for item in widgets))
    return DashboardOut(start=period.start, end=period.end, widgets=list(views))
