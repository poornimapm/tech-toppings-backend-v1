"""A trip's checklist and packing items: one store, a packing item being one with a bag. Adding,
editing and removing one each write an event, so they can be undone from the history. A tick, a
change of state alone, is only kept in the item's own revisions: a day of ticking shouldn't bury
what changed on the trip. Whatever the person changes turns ``edited`` on, and that makes it their
own work: nothing written by rule touches it from then on."""

from __future__ import annotations

import datetime as dt
from typing import Any, ClassVar

from beanie import PydanticObjectId
from fastapi import status

from app.modules.travel.events import (
    CHECKLIST,
    SOURCE_MANUAL,
    ChecklistStore,
    Events,
    Saved,
    edit,
    ensure_in_trip,
    removed,
)
from app.modules.travel.models import (
    ActivityRepository,
    ChecklistItem,
    ChecklistRepository,
    ChecklistSource,
    Trip,
    TripEvent,
    made_with,
)
from app.modules.travel.rules import PackingBag
from app.modules.travel.schemas import TravelChecklistIn, TravelChecklistPatch
from app.modules.travel.trips import Trips, UnknownTravellerError
from app.platform import ActorContext, AppError, VersionConflictError, patched

# In an event's summary: the suggestion the app words the item's title by, while it is one.
KEY = "suggestion_key"
ADDED = "checklist_added"


class UnknownActivityError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code: ClassVar[str] = "TRAVEL_ACTIVITY_UNKNOWN"
    default_message = "That activity isn't on this trip."


class ItemDayMismatchError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code: ClassVar[str] = "TRAVEL_CHECKLIST_DAY_MISMATCH"
    default_message = "An item tied to an activity is on that activity's day."


class Checklist:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.trips = Trips(ctx)
        self.repository = ChecklistRepository(ctx.scope, ctx.clock)
        self.activities = ActivityRepository(ctx.scope, ctx.clock)
        self.events = Events(ctx)

    async def of_trip(
        self,
        trip_id: str,
        day: dt.date | None = None,
        bag: PackingBag | None = None,
        activity_id: str | None = None,
    ) -> list[ChecklistItem]:
        trip = await self.trips.get(trip_id)
        filters = {"day": day.isoformat() if day else None, "bag": bag, "activity_id": activity_id}
        asked = {name: value for name, value in filters.items() if value is not None}
        return await self.repository.of_trip(trip.id, **asked)

    async def ensure_room(self, trip_id: PydanticObjectId | None) -> None:
        await ChecklistStore(self.ctx).ensure_room({"trip_id": trip_id})

    async def _with_trip(self, item_id: str) -> tuple[ChecklistItem, Trip]:
        """The item and its trip: a deleted trip's items are out of reach too."""
        item = await self.repository.get(item_id)
        return item, await self.trips.get(item.trip_id)

    async def _activity_day(
        self, trip: Trip, body: TravelChecklistIn, sent_day: dt.date | None, before: str | None
    ) -> str | None:
        """The day of the activity the item is tied to, which must be on this trip. An item the
        person kept may be tied to one since removed (``before``): it then keeps its own day."""
        activity = await self.activities.find(body.activity_id or "")
        if activity is None or activity.trip_id != trip.id:
            if body.activity_id != before:
                raise UnknownActivityError
            return None
        if sent_day and sent_day.isoformat() != activity.day:
            raise ItemDayMismatchError(details={"day": activity.day})
        return activity.day

    async def _stored(
        self,
        trip: Trip,
        body: TravelChecklistIn,
        sent_day: dt.date | None,
        before: str | None = None,
    ) -> dict[str, Any]:
        """The item as stored, with what it names checked against its trip (rule 6)."""
        if body.traveller_id and body.traveller_id not in {item.id for item in trip.travellers}:
            raise UnknownTravellerError
        fields = body.model_dump(mode="json")
        if body.activity_id:
            fields["day"] = await self._activity_day(trip, body, sent_day, before) or fields["day"]
        if fields["day"]:
            ensure_in_trip(trip, fields["day"])
        return fields

    async def add(
        self, trip_id: str, body: TravelChecklistIn, key: str | None
    ) -> tuple[Saved[ChecklistItem], bool]:
        """The second value says it was made now, not by an earlier request with this key."""
        trip = await self.trips.get(trip_id)
        if earlier := await made_with(self.repository, key):
            return await self.events.made(earlier, ADDED, trip.id), False
        await self.ensure_room(trip.id)
        fields = {
            **await self._stored(trip, body, body.day),
            "trip_id": trip.id,
            "source": ChecklistSource.USER,
        }
        item, made = await self.repository.create_once(fields, key, source=SOURCE_MANUAL)
        if not made:
            return await self.events.made(item, ADDED, trip.id), False
        event = await self.events.record_made(
            trip.id, ADDED, CHECKLIST, item, title=item.title, day=item.day
        )
        return Saved(item, event), True

    async def change(self, item_id: str, body: TravelChecklistPatch) -> Saved[ChecklistItem]:
        """A tick is written on its own, with no event."""
        before, trip = await self._with_trip(item_id)
        if body.revision_no not in (None, before.revision_no):
            raise VersionConflictError(details={"current": before.revision_no})
        checked = patched(TravelChecklistIn, before, body)
        sent_day = body.day if "day" in body.model_fields_set else None
        fields = await self._stored(trip, checked, sent_day, before.activity_id)
        planned = edit(
            CHECKLIST, before, fields, title=before.title, day=before.day, key=before.worded_by
        )
        if not planned.step.fields:
            return Saved(before)
        tick = set(planned.step.fields) == {"state"}
        planned.step.fields["edited"] = True  # not a line the page shows
        event = None
        if tick:
            await ChecklistStore(self.ctx).run(planned.step, SOURCE_MANUAL)
        else:
            event = await self.events.commit(
                before.trip_id,
                "checklist_edited",
                [planned],
                summary={"title": before.title, "day": before.day, KEY: before.worded_by},
            )
        return Saved(await self.repository.get(item_id), event)

    async def delete(self, item_id: str) -> TripEvent | None:
        item = (await self._with_trip(item_id))[0]
        return await self.events.commit(
            item.trip_id,
            "checklist_removed",
            [removed(CHECKLIST, item, title=item.title, day=item.day, key=item.worded_by)],
            summary={"title": item.title, "day": item.day, KEY: item.worded_by},
        )
