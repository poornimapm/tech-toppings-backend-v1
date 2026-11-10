"""One day's plan, for Get Me Ready and Prepare Tomorrow: what is on, what is on its checklist, the
bookings that day needs and how it gets around, and which of the trip's days still hold items to
do. "Today" is the trip's own, at its first destination. A day with nothing on it, or outside the
trip, is an honest empty answer. Only what the person keeps is told: no weather, as there is no
source for it yet."""

from __future__ import annotations

import datetime as dt
from typing import Literal

from app.modules.travel.models import (
    ActivityRepository,
    ActivityStatus,
    BookingRepository,
    ChecklistRepository,
    TripActivity,
    trip_today,
)
from app.modules.travel.rules import rules
from app.modules.travel.schemas import (
    TravelActivityOut,
    TravelBookingOut,
    TravelChecklistOut,
    TravelPrepareOut,
    TravelTransportOut,
)
from app.modules.travel.trips import Trips
from app.platform import ActorContext

OFF_THE_PLAN = (ActivityStatus.SKIPPED, ActivityStatus.REPLACED)


def _day_of(when: Literal["today", "tomorrow"] | dt.date, today: dt.date) -> dt.date:
    if isinstance(when, dt.date):
        return when
    return today if when == "today" else today + dt.timedelta(days=1)


def _transport(activities: list[TripActivity]) -> list[TravelTransportOut]:
    kinds = rules().activities.transport_kinds
    return [
        TravelTransportOut(
            activity_id=str(activity.id),
            title=activity.title,
            kind=activity.kind,
            start_time=activity.start_time,
            location=activity.location,
            transport=activity.transport,
        )
        for activity in activities
        if activity.status not in OFF_THE_PLAN and (activity.transport or activity.kind in kinds)
    ]


class Prepare:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.trips = Trips(ctx)
        self.activities = ActivityRepository(ctx.scope, ctx.clock)
        self.checklist = ChecklistRepository(ctx.scope, ctx.clock)
        self.bookings = BookingRepository(ctx.scope, ctx.clock)

    async def day(
        self, trip_id: str, when: Literal["today", "tomorrow"] | dt.date
    ) -> TravelPrepareOut:
        trip = await self.trips.get(trip_id)
        day = _day_of(when, trip_today(trip, self.ctx))
        iso = day.isoformat()
        planned = await self.activities.of_trip(trip.id, iso)
        items = await self.checklist.of_trip(trip.id, day=iso)
        on_plan = [item.booking_id for item in planned if item.status not in OFF_THE_PLAN]
        bookings = await self.bookings.on_day(trip.id, iso, on_plan)
        open_days = await self.checklist.open_days(trip.id)
        return TravelPrepareOut(
            day=day,
            within_trip=trip.start_date <= iso <= trip.end_date,
            activities=[TravelActivityOut.of(activity) for activity in planned],
            checklist=[TravelChecklistOut.of(item) for item in items],
            bookings=[TravelBookingOut.of(booking) for booking in bookings],
            transport=_transport(planned),
            open_days=[dt.date.fromisoformat(open_day) for open_day in open_days],
        )
