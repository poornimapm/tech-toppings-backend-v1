"""A trip's activities, day by day. Each is one record with its own place in its day; adding,
editing, removing and reordering write only what they name, and every one can be undone from the
history. Marking one done, skipping, replacing or rescheduling it is in changes.py, because those
reach what depends on it."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from typing import Any, ClassVar

from beanie import PydanticObjectId
from fastapi import status

from app.modules.travel.bookings import check_booking
from app.modules.travel.events import (
    ACTIVITY,
    CHECKLIST,
    SOURCE_MANUAL,
    ActivityStore,
    Events,
    Planned,
    Saved,
    edit,
    ensure_in_trip,
    removed,
)
from app.modules.travel.models import (
    ActivityRepository,
    ChecklistItem,
    ChecklistRepository,
    ChecklistState,
    Trip,
    TripActivity,
    TripEvent,
    made_with,
)
from app.modules.travel.schemas import (
    EditedActivity,
    TravelActivityIn,
    TravelActivityPatch,
    TravelFlag,
    TravelReorderIn,
)
from app.modules.travel.trips import Trips
from app.platform import ActorContext, AppError, VersionConflictError, patched

COMPLETED, EDITED = "completed", "edited"  # why an item is listed as kept
ADDED = "activity_added"


class ReorderMismatchError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code: ClassVar[str] = "TRAVEL_REORDER_MISMATCH"
    default_message = "That isn't every activity of this trip's day, each once. Reload and retry."


def stand_down(items: Iterable[ChecklistItem]) -> tuple[list[Planned], list[TravelFlag]]:
    """What an activity going away does to the items tied to it: those untouched and unfinished no
    longer apply, and the person's own work is kept, and listed."""
    planned: list[Planned] = []
    kept: list[TravelFlag] = []
    for item in items:
        done = item.state is ChecklistState.COMPLETED
        if done or item.edited:
            kept.append(
                TravelFlag(
                    entity=CHECKLIST,
                    record_id=str(item.id),
                    title=item.title,
                    reason=COMPLETED if done else EDITED,
                    suggestion_key=item.worded_by,
                )
            )
        else:
            fields = {"state": ChecklistState.NOT_APPLICABLE.value}
            planned.append(
                edit(CHECKLIST, item, fields, title=item.title, day=item.day, key=item.worded_by)
            )
    return planned, kept


class Activities:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.trips = Trips(ctx)
        self.repository = ActivityRepository(ctx.scope, ctx.clock)
        self.checklist = ChecklistRepository(ctx.scope, ctx.clock)
        self.events = Events(ctx)

    async def get(self, activity_id: str) -> TripActivity:
        return (await self.with_trip(activity_id))[0]

    async def with_trip(self, activity_id: str) -> tuple[TripActivity, Trip]:
        """The activity and its trip: a deleted trip's activities are out of reach too."""
        activity = await self.repository.get(activity_id)
        return activity, await self.trips.get(activity.trip_id)

    async def of_trip(self, trip_id: str, day: dt.date | None) -> list[TripActivity]:
        trip = await self.trips.get(trip_id)
        return await self.repository.of_trip(trip.id, day.isoformat() if day else None)

    async def ensure_room(self, trip_id: PydanticObjectId | None) -> None:
        await ActivityStore(self.ctx).ensure_room({"trip_id": trip_id})

    async def add(
        self, trip_id: str, body: TravelActivityIn, key: str | None
    ) -> tuple[Saved[TripActivity], bool]:
        """Last in its day. The second value says it was made now, not by an earlier request
        with this key."""
        trip = await self.trips.get(trip_id)
        if earlier := await made_with(self.repository, key):
            return await self.events.made(earlier, ADDED, trip.id), False
        day = body.day.isoformat()
        ensure_in_trip(trip, day)
        await check_booking(self.ctx, trip.id, body.booking_id)
        await self.ensure_room(trip.id)
        fields: dict[str, Any] = {
            **body.model_dump(mode="json"),
            "trip_id": trip.id,
            "position": await self.repository.next_position(trip.id, day),
        }
        activity, made = await self.repository.create_once(fields, key, source=SOURCE_MANUAL)
        if not made:
            return await self.events.made(activity, ADDED, trip.id), False
        event = await self.events.record_made(
            trip.id, ADDED, ACTIVITY, activity, title=activity.title, day=day
        )
        return Saved(activity, event), True

    async def change(self, activity_id: str, body: TravelActivityPatch) -> Saved[TripActivity]:
        before = await self.get(activity_id)
        if body.revision_no != before.revision_no:
            raise VersionConflictError(details={"current": before.revision_no})
        checked = patched(EditedActivity, before, body)
        if checked.booking_id != before.booking_id:
            await check_booking(self.ctx, before.trip_id, checked.booking_id)
        planned = edit(
            ACTIVITY, before, checked.model_dump(mode="json"), title=before.title, day=before.day
        )
        event = await self.events.commit(
            before.trip_id,
            "activity_edited",
            [planned],
            summary={"title": before.title, "day": before.day},
        )
        return Saved(await self.get(activity_id), event)

    async def delete(self, activity_id: str) -> TripEvent | None:
        """Its checklist items are treated as when it is skipped, so nothing typed is lost, and
        they come back with it."""
        activity = await self.get(activity_id)
        items, kept = stand_down(await self.checklist.tied_to(activity))
        return await self.events.commit(
            activity.trip_id,
            "activity_deleted",
            [removed(ACTIVITY, activity, title=activity.title, day=activity.day), *items],
            summary={"title": activity.title, "day": activity.day, "kept": len(kept)},
        )

    async def reorder(self, trip_id: str, body: TravelReorderIn) -> Saved[list[TripActivity]]:
        """A new order for one day. The ids are checked against that day of this trip, and only
        what really moved is written: nothing on any other day."""
        trip = await self.trips.get(trip_id)
        day = body.day.isoformat()
        current = {
            str(activity.id): activity for activity in await self.repository.of_trip(trip.id, day)
        }
        sent = body.activity_ids
        if len(sent) != len(current) or set(sent) != set(current):
            raise ReorderMismatchError(details={"day": day})
        planned = [
            edit(ACTIVITY, current[id_], {"position": place}, title=current[id_].title, day=day)
            for place, id_ in enumerate(sent)
        ]
        event = await self.events.commit(
            trip.id, "activities_reordered", planned, summary={"day": day}
        )
        return Saved(await self.repository.of_trip(trip.id, day), event)
