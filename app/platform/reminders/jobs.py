from __future__ import annotations

from app.platform.context import ActorContext
from app.platform.jobs.contract import job
from app.platform.reminders.service import Reminders


@job("each")
async def reminders_deliver(ctx: ActorContext) -> int:
    """Tell the reminders that have come due. Cheap when none have: one indexed read."""
    return await Reminders(ctx).deliver_due()


@job("each")
async def reminders_push(ctx: ActorContext) -> int:
    """Push what was just told to the user's browsers; after delivering, so in the same
    catch-up. A job of its own: a slow push service never holds up the inbox. Cheap when no
    push is owed: one indexed read."""
    return await Reminders(ctx).push_due()
