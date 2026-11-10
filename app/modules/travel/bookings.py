"""A trip's bookings, as the person entered them: the app never makes one up, and never says one was
extracted or verified when it wasn't. Adding, editing and removing one each write an event, so they
can be undone from the history. An activity links only to a booking of its own trip (rule 6), and a
booking stays while an activity links to it."""

from __future__ import annotations

from typing import Any, ClassVar

from beanie import PydanticObjectId
from fastapi import status

from app.modules.travel.events import (
    BOOKING,
    SOURCE_MANUAL,
    BookingStore,
    Events,
    Saved,
    edit,
    removed,
)
from app.modules.travel.models import (
    ActivityRepository,
    Booking,
    BookingRepository,
    Trip,
    TripEvent,
    made_with,
)
from app.modules.travel.schemas import BookingFields, TravelBookingIn, TravelBookingPatch
from app.modules.travel.trips import Trips
from app.platform import ActorContext, AppError, ConflictError, VersionConflictError, patched

ADDED = "booking_added"


class UnknownBookingError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code: ClassVar[str] = "TRAVEL_BOOKING_UNKNOWN"
    default_message = "That booking isn't on this trip."


class BookingInUseError(ConflictError):
    code: ClassVar[str] = "TRAVEL_BOOKING_IN_USE"
    default_message = "Activities still link to that booking: unlink them first."


async def check_booking(
    ctx: ActorContext, trip_id: PydanticObjectId | None, booking_id: str | None
) -> None:
    """A booking an activity links to is one of the same trip. Another person's, another trip's
    or one removed answer alike."""
    if booking_id is None:
        return
    booking = await BookingRepository(ctx.scope, ctx.clock).find(booking_id)
    if booking is None or booking.trip_id != trip_id:
        raise UnknownBookingError


class Bookings:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.trips = Trips(ctx)
        self.repository = BookingRepository(ctx.scope, ctx.clock)
        self.activities = ActivityRepository(ctx.scope, ctx.clock)
        self.events = Events(ctx)

    async def of_trip(self, trip_id: str) -> list[Booking]:
        trip = await self.trips.get(trip_id)
        return await self.repository.of_trip(trip.id)

    async def with_trip(self, booking_id: str) -> tuple[Booking, Trip]:
        """The booking and its trip: a deleted trip's bookings are out of reach too."""
        booking = await self.repository.get(booking_id)
        return booking, await self.trips.get(booking.trip_id)

    async def get(self, booking_id: str) -> Booking:
        return (await self.with_trip(booking_id))[0]

    async def add(
        self, trip_id: str, body: TravelBookingIn, key: str | None
    ) -> tuple[Saved[Booking], bool]:
        """The second value says it was made now, not by an earlier request with this key."""
        trip = await self.trips.get(trip_id)
        if earlier := await made_with(self.repository, key):
            return await self.events.made(earlier, ADDED, trip.id), False
        await BookingStore(self.ctx).ensure_room({"trip_id": trip.id})
        fields: dict[str, Any] = {**body.model_dump(mode="json"), "trip_id": trip.id}
        booking, made = await self.repository.create_once(fields, key, source=SOURCE_MANUAL)
        if not made:
            return await self.events.made(booking, ADDED, trip.id), False
        event = await self.events.record_made(trip.id, ADDED, BOOKING, booking, title=booking.title)
        await self.trips.noted("booking_added", title=booking.title, trip=trip.title)
        return Saved(booking, event), True

    async def change(self, booking_id: str, body: TravelBookingPatch) -> Saved[Booking]:
        before = await self.get(booking_id)
        if body.revision_no != before.revision_no:
            raise VersionConflictError(details={"current": before.revision_no})
        fields = patched(BookingFields, before, body).model_dump(mode="json")
        if body.status is not None:
            fields["status"] = body.status
        planned = edit(BOOKING, before, fields, title=before.title)
        event = await self.events.commit(
            before.trip_id, "booking_edited", [planned], summary={"title": before.title}
        )
        return Saved(await self.get(booking_id), event)

    async def delete(self, booking_id: str) -> TripEvent | None:
        booking = await self.get(booking_id)
        linking = await self.activities.linking(str(booking.id))
        if linking:
            raise BookingInUseError(details={"activity_ids": linking})
        return await self.events.commit(
            booking.trip_id,
            "booking_removed",
            [removed(BOOKING, booking, title=booking.title)],
            summary={"title": booking.title},
        )
