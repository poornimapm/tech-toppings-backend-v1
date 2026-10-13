"""Dashboard widgets about the assistant."""

from __future__ import annotations

from app.core.daterange import DateRange
from app.platform.agents.models import AgentRun
from app.platform.agents.runtime import AgentRunRepository
from app.platform.context import ActorContext
from app.platform.dashboard.contract import Series, daily_counts, widget


@widget("series")
async def agents_requests(ctx: ActorContext, period: DateRange) -> Series:
    """Requests to the assistant per day."""
    criteria = AgentRunRepository(ctx.scope, ctx.clock).query()
    return Series(points=await daily_counts(AgentRun, criteria, period))
