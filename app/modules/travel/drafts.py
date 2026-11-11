"""Applying AI drafts through Travel's own engine (Basic AI). Each is checked by the same schemas
and rules as the person's own input, is a preview until ``apply``, and applying records one event
that can be undone:

- a day's draft (``day_drafted``): its activities go after the day's own, so nothing there moves or
  goes;
- an activity's items (``checklist_ai_suggested``): stored as the assistant's (``source``
  assistant, key ``ai:{activity_id}:{words}``) and offered as suggestions are, so an item already
  there, or removed by the person, is never touched or brought back (rule 4).

The model never applies anything: its client tools (tools.py) run these previews, and the trip's
pages apply what the person confirms, naming the run (``ai_run_id``) so the event says AI made it.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence

from app.modules.travel.ai import DAY_DRAFT, ITEMS_DRAFT, AiDraftAppliedError, drafted_by
from app.modules.travel.changes import ActivityStateError
from app.modules.travel.checklist import UnknownActivityError
from app.modules.travel.events import (
    ACTIVITY,
    CHECKLIST,
    ActivitiesFullError,
    Events,
    Planned,
    added,
    ensure_in_trip,
)
from app.modules.travel.models import (
    ActivityRepository,
    ActivityStatus,
    ChecklistSource,
    Trip,
    TripActivity,
)
from app.modules.travel.rules import rules
from app.modules.travel.schemas import (
    DraftedActivity,
    DraftedItem,
    TravelChangeOut,
    TravelChecklistAssistIn,
    TravelDayDraftIn,
    TravelSuggestOut,
)
from app.modules.travel.suggestions import KEY, SHOWN, Suggestions
from app.modules.travel.trips import Trips
from app.platform import ActorContext

DAY_DRAFTED = "day_drafted"
AI_SUGGESTED = "checklist_ai_suggested"
# What a day's preview says of each activity, besides its title and day.
ACTIVITY_SHOWN = (
    "kind",
    "start_time",
    "duration_minutes",
    "location",
    "transport",
    "notes",
    "estimated_cost_minor",
)


def words_key(activity: TripActivity, title: str) -> str:
    """An AI item's key: its activity and its words in any script (letters, marks and digits),
    so the same item drafted again has the same key."""
    kept = "".join(ch if unicodedata.category(ch)[0] in "LMN" else " " for ch in title.casefold())
    return f"ai:{activity.id}:{'_'.join(kept.split()) or 'item'}"


async def activity_on(ctx: ActorContext, trip: Trip, activity_id: str) -> TripActivity:
    """An activity of this trip, still planned: one AI may draft for."""
    activity = await ActivityRepository(ctx.scope, ctx.clock).find(activity_id)
    if activity is None or activity.trip_id != trip.id:
        raise UnknownActivityError
    if activity.status is not ActivityStatus.PLANNED:
        raise ActivityStateError(details={"status": activity.status})
    return activity


class DayDrafts:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.trips = Trips(ctx)
        self.activities = ActivityRepository(ctx.scope, ctx.clock)
        self.events = Events(ctx)

    async def _planned(
        self, trip: Trip, day: str, drafts: Sequence[DraftedActivity]
    ) -> list[Planned]:
        """After the day's last activity, in the draft's order, while the trip has room for all."""
        if await self.activities.count_of(trip.id) + len(drafts) > rules().activities.max_per_trip:
            raise ActivitiesFullError
        first = await self.activities.next_position(trip.id, day)
        return [
            added(
                ACTIVITY,
                {
                    **draft.model_dump(mode="json"),
                    "trip_id": trip.id,
                    "day": day,
                    "position": first + place,
                },
                title=draft.title,
                day=day,
                show=ACTIVITY_SHOWN,
            )
            for place, draft in enumerate(drafts)
        ]

    async def _applied(self, trip: Trip, run_id: str) -> bool:
        """Whether this run's draft is on the trip already: added, and not undone since."""
        found = {"trip_id": trip.id, "kind": DAY_DRAFTED, "ai_run_id": run_id, "undone_at": None}
        return await self.events.repository.first(found) is not None

    async def run(self, trip_id: str, body: TravelDayDraftIn) -> TravelChangeOut:
        """A run's draft is added once (a retry, another tab): again only after its undo."""
        trip = await self.trips.get(trip_id)
        run_id = await drafted_by(self.ctx, body.ai_run_id, DAY_DRAFT, trip)
        day = body.day.isoformat()
        ensure_in_trip(trip, day)
        planned = await self._planned(trip, day, body.activities)
        if not body.apply:
            return TravelChangeOut.of([item.shown for item in planned], [], [])
        if run_id and await self._applied(trip, run_id):
            raise AiDraftAppliedError(details={"ai_run_id": run_id})
        event = await self.events.commit(
            trip.id,
            DAY_DRAFTED,
            planned,
            summary={"day": day, "count": len(planned)},
            ai_run_id=run_id,
        )
        return TravelChangeOut.of(
            event.changes if event else [], [], [], str(event.id) if event else None
        )


def _item(trip: Trip, activity: TripActivity, draft: DraftedItem, key: str) -> Planned:
    fields = {
        "trip_id": trip.id,
        "title": draft.title,
        "day": activity.day,
        "activity_id": str(activity.id),
        "bag": draft.bag.value if draft.bag else None,
        "quantity": draft.quantity,
        "source": ChecklistSource.ASSISTANT.value,
        KEY: key,
    }
    return added(CHECKLIST, fields, title=draft.title, day=activity.day, show=SHOWN)


class AiItems:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.trips = Trips(ctx)
        self.suggestions = Suggestions(ctx)

    async def run(self, trip_id: str, body: TravelChecklistAssistIn) -> TravelSuggestOut:
        """Tied to the activity, on its day; an item drafted twice is added once."""
        trip = await self.trips.get(trip_id)
        run_id = await drafted_by(self.ctx, body.ai_run_id, ITEMS_DRAFT, trip)
        activity = await activity_on(self.ctx, trip, body.activity_id)
        wanted: dict[str, Planned] = {}
        for draft in body.items:
            key = words_key(activity, draft.title)
            wanted.setdefault(key, _item(trip, activity, draft, key))
        return await self.suggestions.offer(
            trip,
            AI_SUGGESTED,
            list(wanted.values()),
            apply=body.apply,
            summary={"title": activity.title, "day": activity.day, "activity_id": str(activity.id)},
            ai_run_id=run_id,
        )
