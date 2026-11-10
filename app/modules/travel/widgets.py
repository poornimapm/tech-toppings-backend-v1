"""Dashboard widgets: the next trip, the one under way first, whatever the period shown."""

from __future__ import annotations

from app.modules.travel.models import MODULE, TripPhase
from app.modules.travel.trips import Trips
from app.platform import ActorContext, DateRange, Item, Items, PageParams, widget


@widget("list")
async def travel_next_trip(
    ctx: ActorContext,
    period: DateRange,  # noqa: ARG001 - the next trip from today, whatever the period
) -> Items:
    trips = Trips(ctx)
    for phase in (TripPhase.ACTIVE, TripPhase.UPCOMING):
        found, _ = await trips.page(PageParams(1, 1), phase)
        if found:
            trip = found[0]
            params = {"module": MODULE, "title": trip.title, "trip_id": str(trip.id)}
            dates = {"start": trip.start_date, "end": trip.end_date}
            # Worded on the app by phase: travel.trip_active (under way) or travel.trip_upcoming.
            return Items(items=[Item(code=f"{MODULE}.trip_{phase}", params=params | dates)])
    return Items(items=[])
