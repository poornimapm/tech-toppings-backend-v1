from __future__ import annotations

from app.platform.context import ActorContext
from app.platform.jobs.contract import job
from app.platform.reminders.service import Reminders


@job("each")
async def reminders_deliver(ctx: ActorContext) -> int:
    """Tell the reminders that have come due. Cheap when none have: one indexed read."""
    return await Reminders(ctx).deliver_due()
