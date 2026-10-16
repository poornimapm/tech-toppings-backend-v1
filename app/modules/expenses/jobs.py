from __future__ import annotations

from app.modules.expenses.features.recurring.service import Recurring
from app.platform import ActorContext, job


@job("daily")
async def expenses_recurring(ctx: ActorContext) -> int:
    """Add the entries of repeating rules that have come due. Bills need nothing here: their
    reminders are scheduled when they are saved or paid."""
    return await Recurring(ctx).materialize()
