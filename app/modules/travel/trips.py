"""Trips: where, when, who with and how. A trip's mode is one field of it, so switching mode leaves
every other record as it is. Saving Premium stays locked (``premium_allowed``). A trip's "today" is
at its first destination, else where its person is: a trip across several time zones is only as
precise as that (a known limit)."""

from __future__ import annotations

import datetime as dt
import re
import uuid
from types import SimpleNamespace
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from beanie import PydanticObjectId
from fastapi import status

from app.modules.travel.events import SOURCE_MANUAL, TRIP, Events, Saved, TripStore, edit
from app.modules.travel.models import (
    MODULE,
    Trip,
    TripMode,
    TripPhase,
    TripRepository,
    made_with,
    trip_today,
)
from app.modules.travel.reminder import sync_reminder
from app.modules.travel.rules import rules
from app.modules.travel.schemas import (
    TravellerIn,
    TravelTripOut,
    TravelTripRecorded,
    TripIn,
    TripPatch,
)
from app.platform import (
    ActivityLog,
    ActorContext,
    AppError,
    ForbiddenError,
    PageParams,
    VersionConflictError,
    patched,
)

FIRST_ZONE = "destinations.0.timezone"  # a trip's today is told by it
CREATED = "trip_created"


class PremiumLockedError(ForbiddenError):
    code: ClassVar[str] = "TRAVEL_PREMIUM_LOCKED"
    default_message = "Premium isn't available yet. Choose Static or Basic."


class TripDatesError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code: ClassVar[str] = "TRAVEL_TRIP_DATES_INVALID"
    default_message = "A trip ends on or after its first day, and can only be so long."


class UnknownTravellerError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code: ClassVar[str] = "TRAVEL_TRAVELLER_UNKNOWN"
    default_message = "That traveller isn't on this trip."


def premium_allowed(ctx: ActorContext) -> bool:  # noqa: ARG001 - who may is decided in Phase 22
    """Whether this person may save a trip in Premium mode: nobody yet."""
    return False


def phase_on(trip: Trip, today: dt.date) -> TripPhase:
    day = today.isoformat()
    if day < trip.start_date:
        return TripPhase.UPCOMING
    return TripPhase.PAST if day > trip.end_date else TripPhase.ACTIVE


def _within(phase: TripPhase, today: dt.date) -> dict[str, Any]:
    """The trips in this phase, whose today is ``today``."""
    day = today.isoformat()
    if phase is TripPhase.UPCOMING:
        return {"start_date": {"$gt": day}}
    if phase is TripPhase.PAST:
        return {"end_date": {"$lt": day}}
    return {"start_date": {"$lte": day}, "end_date": {"$gte": day}}


async def in_phase(
    repository: TripRepository, phase: TripPhase, now: dt.datetime, today: dt.date
) -> dict[str, Any]:
    """The trips in this phase, now. Trips differ in what today is: one clause for each time zone
    they start in, and one for those with none (whose today is ``today``, their person's). That
    one can't ask for a null: through an array, a path with an index also reads a field named "0"
    in each element, which is missing in every trip."""
    found = await Trip.get_pymongo_collection().distinct(FIRST_ZONE, repository.query())
    zones = [zone for zone in found if zone]
    clauses = [
        {FIRST_ZONE: zone, **_within(phase, now.astimezone(ZoneInfo(zone)).date())}
        for zone in zones
    ]
    return {"$or": [{FIRST_ZONE: {"$nin": zones}, **_within(phase, today)}, *clauses]}


def _check_dates(body: TripIn) -> None:
    if body.end_date < body.start_date:
        raise TripDatesError(details={"reason": "end_before_start"})
    longest = rules().trips.max_days
    if (body.end_date - body.start_date).days + 1 > longest:
        raise TripDatesError(details={"reason": "too_long", "max_days": longest})


def _travellers(sent: list[TravellerIn], before: Trip | None) -> list[dict[str, Any]]:
    """Everyone as stored: a new person gets an id, and an id given must be one of this trip's."""
    known = {traveller.id for traveller in before.travellers} if before else set()
    given = [traveller.id for traveller in sent if traveller.id]
    if len(set(given)) != len(given) or not known.issuperset(given):
        raise UnknownTravellerError
    return [
        {**traveller.model_dump(mode="json"), "id": traveller.id or uuid.uuid4().hex}
        for traveller in sent
    ]


class Trips:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.repository = TripRepository(ctx.scope, ctx.clock)
        self.events = Events(ctx)
        self.feed = ActivityLog(ctx.scope, ctx.clock, ctx.settings)

    async def noted(self, kind: str, **params: Any) -> None:
        """In the person's activity feed, as other modules note theirs: what happened, worded on
        the app by its kind, with ``title`` the record's own words."""
        await self.feed.record(f"{MODULE}.{kind}", module=MODULE, **params)

    def _fields(self, trip: Trip) -> dict[str, Any]:
        return {
            **trip.model_dump(),
            "id": str(trip.id),
            "phase": phase_on(trip, trip_today(trip, self.ctx)),
        }

    def out(self, trip: Trip) -> TravelTripOut:
        return TravelTripOut.model_validate(self._fields(trip))

    def recorded(self, saved: Saved[Trip]) -> TravelTripRecorded:
        """What a write to a trip answers: the trip, and the event it recorded."""
        return TravelTripRecorded.model_validate(
            {**self._fields(saved.record), "event_id": saved.event_id}
        )

    def _stored(self, body: TripIn, before: Trip | None = None) -> dict[str, Any]:
        _check_dates(body)
        newly_premium = body.mode is TripMode.PREMIUM and (
            before is None or before.mode != body.mode
        )
        if newly_premium and not premium_allowed(self.ctx):
            raise PremiumLockedError
        return {
            **body.model_dump(mode="json", exclude={"travellers"}),
            "currency": body.currency or self.ctx.user.base_currency,
            "travellers": _travellers(body.travellers, before),
        }

    async def add(self, body: TripIn, key: str | None) -> tuple[Saved[Trip], bool]:
        """The new trip, or the one an earlier request with this key made (then ``False``)."""
        if earlier := await made_with(self.repository, key):
            return await self.events.made(earlier, CREATED), False
        await TripStore(self.ctx).ensure_room({})
        fields = self._stored(body)
        trip, made = await self.repository.create_once(fields, key, source=SOURCE_MANUAL)
        if not made:
            return await self.events.made(trip, CREATED), False
        event = await self.events.record(trip.id, CREATED, summary={"title": trip.title})
        await sync_reminder(self.ctx, trip)
        await self.noted("trip_added", title=trip.title)
        return Saved(trip, event), True

    async def page(
        self, params: PageParams, phase: TripPhase | None, text: str | None = None
    ) -> tuple[list[Trip], int]:
        """Soonest first; past trips, and all of them together, latest first. ``text``: those
        whose title has it, in any case."""
        now, today = self.ctx.clock.now(), self.ctx.today()
        filters = await in_phase(self.repository, phase, now, today) if phase else {}
        if text:
            filters["title"] = {"$regex": re.escape(text), "$options": "i"}
        ahead = phase in (TripPhase.UPCOMING, TripPhase.ACTIVE)
        return await self.repository.list(
            params, [("start_date", 1 if ahead else -1), ("_id", 1)], filters
        )

    async def get(self, trip_id: str | PydanticObjectId) -> Trip:
        return await self.repository.get(trip_id)

    async def change(self, trip_id: str, body: TripPatch) -> Saved[Trip]:
        before = await self.get(trip_id)
        if body.revision_no != before.revision_no:
            raise VersionConflictError(details={"current": before.revision_no})
        # As plain values: the destinations and travellers are checked again as sent.
        checked = patched(TripIn, SimpleNamespace(**before.model_dump()), body)
        planned = edit(TRIP, before, self._stored(checked, before), title=before.title)
        if set(planned.step.fields) == {"mode"}:
            # A mode switch touches no other record, the history included; its revision keeps it.
            switched = await self.repository.update(
                trip_id, planned.step.fields, expected=before.revision_no, source=SOURCE_MANUAL
            )
            return Saved(switched)
        event = await self.events.commit(
            before.id, "trip_edited", [planned], summary={"title": before.title}
        )
        after = await self.get(trip_id)
        if event:
            await self.noted("trip_updated", title=after.title)
        return Saved(after, event)

    async def delete(self, trip_id: str) -> None:
        """Soft: everything in it stays, out of reach until the trip is brought back."""
        trip = await self.get(trip_id)
        await self.repository.soft_delete(trip_id, source=SOURCE_MANUAL)
        await self.events.record(trip.id, "trip_deleted", summary={"title": trip.title})
        await sync_reminder(self.ctx, await self.repository.get(trip_id, include_deleted=True))
        await self.noted("trip_deleted", title=trip.title)

    async def undelete(self, trip_id: str) -> Saved[Trip]:
        """Back with everything in it, while there is room for one more of the person's trips.
        One that is there already is answered as it is, with no event."""
        before = await self.repository.get(trip_id, include_deleted=True)
        if before.deleted_at is None:
            return Saved(before)
        await TripStore(self.ctx).ensure_room(before.model_dump())
        trip = await self.repository.undelete(trip_id, source=SOURCE_MANUAL)
        event = await self.events.record(trip.id, "trip_restored", summary={"title": trip.title})
        await sync_reminder(self.ctx, trip)
        await self.noted("trip_restored", title=trip.title)
        return Saved(trip, event)
