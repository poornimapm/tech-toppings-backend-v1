"""The one reminder a trip can ask for (``remind_prepare``): the day before it starts, told through
``Reminders``. It follows the trip: it moves with the dates and goes with the setting or the trip.
Every write to a trip passes here, an undo's too, once its change is recorded
(``events.TripStore.follow``)."""

from __future__ import annotations

from datetime import date

from app.modules.travel.models import MODULE, Trip, trip_today
from app.platform import ActorContext, Reminders

REMINDER = f"{MODULE}.prepare_due"


async def sync_reminder(ctx: ActorContext, trip: Trip) -> None:
    """Scheduled while it is wanted and its day isn't gone: a trip starting today has no day before
    left to be reminded on."""
    reminders, ref = Reminders(ctx), str(trip.id)
    start = date.fromisoformat(trip.start_date)
    if trip.remind_prepare and trip.deleted_at is None and trip_today(trip, ctx) < start:
        await reminders.schedule(
            REMINDER,
            module=MODULE,
            ref_id=ref,
            due_on=start,
            days_before=1,
            title=trip.title,
            trip_id=ref,
        )
    else:
        await reminders.cancel(module=MODULE, ref_id=ref)
