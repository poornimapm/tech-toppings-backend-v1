"""Suggestions for a trip's checklist and packing, by rule (data/travel.yaml): from the activities
it really has and how long it is, and nothing about any place or operator. They are stored as what
they are (``source`` rule, with a ``suggestion_key``), for the page to label.

The person's word beats them: asking again only adds keys not there yet, so an item edited,
completed or removed is never touched, and one removed doesn't come back (rule 4). Like a change,
it's a preview until ``apply``, and applying records one event, which can be undone."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

from app.modules.travel.events import (
    CHECKLIST,
    ChecklistFullError,
    Events,
    Planned,
    added,
    ensure_in_trip,
)
from app.modules.travel.models import (
    ActivityRepository,
    ActivityStatus,
    ChecklistRepository,
    ChecklistSource,
    Trip,
    TripActivity,
    trip_today,
)
from app.modules.travel.rules import SuggestedItem, rules
from app.modules.travel.schemas import TravelSuggestIn, TravelSuggestOut
from app.modules.travel.trips import Trips
from app.platform import ActorContext

KIND = "checklist_suggested"
KEY = "suggestion_key"
# What a preview says of each, besides its title and day.
SHOWN = ("bag", "quantity", "activity_id", KEY)


def _trip_days(trip: Trip) -> int:
    start, end = dt.date.fromisoformat(trip.start_date), dt.date.fromisoformat(trip.end_date)
    return (end - start).days + 1


def _item(trip: Trip, rule: SuggestedItem, key: str, **place: Any) -> Planned:
    fields = {
        "trip_id": trip.id,
        "title": rule.title,
        "day": None,
        "activity_id": None,
        "bag": rule.bag.value if rule.bag else None,
        "quantity": rule.quantity_for(_trip_days(trip)),
        "source": ChecklistSource.RULE.value,
        KEY: key,
        **place,
    }
    return added(CHECKLIST, fields, title=rule.title, day=fields["day"], show=SHOWN, key=key)


def _of_activities(
    trip: Trip, activities: list[TripActivity], *, packing: bool
) -> Iterator[Planned]:
    """Each activity's own, tied to it. A packing list wants only what goes in a bag."""
    by_kind = rules().suggestions.activities
    for activity in activities:
        for rule in by_kind.get(activity.kind, ()):
            if rule.bag or not packing:
                key = f"activity:{activity.id}:{rule.key}"
                yield _item(trip, rule, key, day=activity.day, activity_id=str(activity.id))


def _of_trip(trip: Trip) -> Iterator[Planned]:
    for rule in rules().suggestions.packing:
        yield _item(trip, rule, f"packing:{rule.key}")


class Suggestions:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.trips = Trips(ctx)
        self.activities = ActivityRepository(ctx.scope, ctx.clock)
        self.checklist = ChecklistRepository(ctx.scope, ctx.clock)
        self.events = Events(ctx)

    async def _wanted(self, trip: Trip, body: TravelSuggestIn) -> list[Planned]:
        """From what is still to come: an activity done, skipped or replaced needs nothing, and
        neither does a day gone by. The list for the whole trip belongs to no one day."""
        day = body.day.isoformat() if body.day else None
        if day:
            ensure_in_trip(trip, day)
        today = trip_today(trip, self.ctx).isoformat()
        ahead = [
            activity
            for activity in await self.activities.of_trip(trip.id, day)
            if activity.status is ActivityStatus.PLANNED and activity.day >= today
        ]
        wanted = list(_of_activities(trip, ahead, packing=body.packing))
        return [*wanted, *_of_trip(trip)] if body.packing and day is None else wanted

    async def run(self, trip_id: str, body: TravelSuggestIn) -> TravelSuggestOut:
        trip = await self.trips.get(trip_id)
        taken = set(await self.checklist.taken_keys(trip.id))
        fresh = [
            item for item in await self._wanted(trip, body) if item.step.fields[KEY] not in taken
        ]
        limits = rules()
        room = limits.checklist.max_per_trip - await self.checklist.count_of(trip.id)
        if fresh and room <= 0:
            raise ChecklistFullError
        batch = fresh[: min(room, limits.suggestions.max_per_run)]
        more = len(fresh) > len(batch)
        if not body.apply:
            return TravelSuggestOut.of([item.shown for item in batch], [], [], more=more)
        event = await self.events.commit(
            trip.id,
            KIND,
            batch,
            summary={
                "count": len(batch),
                "packing": body.packing,
                "day": body.day.isoformat() if body.day else None,
            },
        )
        changes = event.changes if event else []
        event_id = str(event.id) if event else None
        return TravelSuggestOut.of(changes, [], [], event_id, more=more)
