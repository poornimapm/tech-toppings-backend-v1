"""Dashboard widgets fed by the activity feed."""

from __future__ import annotations

from app.core.daterange import DateRange
from app.core.pagination import PageParams
from app.core.repository import NEWEST_FIRST
from app.platform.activity.log import Activity, ActivityLog
from app.platform.context import ActorContext
from app.platform.dashboard.contract import Item, Items, Kpi, count_in, widget


def _log(ctx: ActorContext) -> ActivityLog:
    return ActivityLog(ctx.scope, ctx.clock, ctx.settings)


@widget("kpi")
async def activity_actions(ctx: ActorContext, period: DateRange) -> Kpi:
    """How much happened in the period, against the period before it."""
    criteria = _log(ctx).query()
    return Kpi(
        value=await count_in(Activity, criteria, period),
        previous=await count_in(Activity, criteria, period.previous()),
    )


@widget("list")
async def activity_recent(ctx: ActorContext, period: DateRange) -> Items:
    """The latest entries in the period."""
    page = PageParams(page=1, page_size=ctx.settings.dashboard.activity_widget_items)
    entries, _ = await _log(ctx).list(page, NEWEST_FIRST, period.match())
    return Items(
        items=[Item(code=entry.type, params=entry.params, at=entry.created_at) for entry in entries]
    )
