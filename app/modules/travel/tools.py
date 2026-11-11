"""Agent tools for Travel's Basic AI, used by the ``travel`` agent's skills only (ADR-0026). Thin
over the same services as the API, scoped to the caller, and only for a trip in Basic
(``ensure_ai``): another person's trip is not found, and a Static one is refused.

The model reads only what the plan allows it: a trip's places, dates, kind, pace, interests and
how many travel, and its activities, items and budget figures. Never a name, a passport detail, a
residence, food or access needs, a note, a booking's details, or who paid what. What it makes are
drafts handed to the trip's pages (client tools), each checked by running the preview the page
runs, so by the same rules as the person's own input; the person applies it there. No tool here
writes anything. Money goes both ways in the currency's main unit, as people say it (as in the
expenses tools), so the model never has to know a currency's decimals."""

from __future__ import annotations

from collections import Counter
from typing import Annotated, Any, Literal

from pydantic import Field

from app.modules.travel.activities import Activities
from app.modules.travel.ai import ensure_ai
from app.modules.travel.budget import Budget
from app.modules.travel.changes import Changes
from app.modules.travel.checklist import UnknownActivityError
from app.modules.travel.drafts import AiItems, DayDrafts
from app.modules.travel.events import ensure_in_trip
from app.modules.travel.models import (
    ActivityRepository,
    ChecklistItem,
    ChecklistRepository,
    Trip,
    TripActivity,
    trip_today,
)
from app.modules.travel.schemas import (
    AiActivities,
    DayText,
    DraftedItems,
    TravelChangeDraft,
    TravelChangeIn,
    TravelChecklistAssistIn,
    TravelDayDraftIn,
    TravelTripDraft,
    TripIn,
)
from app.modules.travel.trips import Trips, check_dates, phase_on
from app.platform import ActorContext, in_major, tool

TripId = Annotated[str, Field(min_length=1, max_length=32, description="The trip's id")]
ActivityId = Annotated[str, Field(min_length=1, max_length=32, description="The activity's id")]
Day = Annotated[DayText, Field(description="YYYY-MM-DD, one of the trip's days")]
Section = Literal["overview", "itinerary", "prepare", "packing", "budget", "bookings", "history"]
# What the budget tool says: figures, never who paid or what a cost was called.
BUDGET_FIGURES = {
    "currency",
    "budget_minor",
    "estimated_minor",
    "actual_minor",
    "remaining_minor",
    "categories",
}


async def _trip(ctx: ActorContext, trip_id: str) -> Trip:
    return ensure_ai(await Trips(ctx).get(trip_id))


def _activity(activity: TripActivity, currency: str) -> dict[str, Any]:
    """Not its notes or what really happened (the person's words), nor its booking's details."""
    shown = {
        "id": str(activity.id),
        "day": activity.day,
        "title": activity.title,
        "kind": activity.kind,
        "start_time": activity.start_time,
        "duration_minutes": activity.duration_minutes,
        "location": activity.location,
        "transport": activity.transport,
        "estimated_cost_minor": activity.estimated_cost_minor,
        "confirmed": activity.confirmed,
        "booked": activity.booking_id is not None,
        "status": activity.status,
    }
    return dict(in_major(shown, currency))


def _item(item: ChecklistItem) -> dict[str, Any]:
    """Not the traveller it is for."""
    return {
        "id": str(item.id),
        "title": item.title,
        "state": item.state,
        "bag": item.bag,
        "quantity": item.quantity,
        "activity_id": item.activity_id,
    }


@tool("read")
async def travel_trip(ctx: ActorContext, trip_id: TripId) -> dict[str, Any]:
    """A trip: its places, dates, kind, pace, interests, how many travel, its currency, today on
    the trip, and how many activities each day holds."""
    trip = await _trip(ctx, trip_id)
    today = trip_today(trip, ctx)
    activities = await ActivityRepository(ctx.scope, ctx.clock).of_trip(trip.id)
    return {
        "id": str(trip.id),
        "destinations": [place.model_dump() for place in trip.destinations],
        "departure_city": trip.departure_city,
        "start_date": trip.start_date,
        "end_date": trip.end_date,
        "today": today.isoformat(),
        "phase": phase_on(trip, today),
        "trip_type": trip.trip_type,
        "pace": trip.pace,
        "interests": trip.interests,
        "travellers": len(trip.travellers),
        "currency": trip.currency,
        "activities_by_day": dict(sorted(Counter(item.day for item in activities).items())),
    }


@tool("read")
async def travel_day(ctx: ActorContext, trip_id: TripId, day: Day) -> dict[str, Any]:
    """One day of a trip: its activities in order (an estimated cost in the trip's currency's
    main unit), and its checklist and packing items."""
    trip = await _trip(ctx, trip_id)
    ensure_in_trip(trip, day)
    activities = await ActivityRepository(ctx.scope, ctx.clock).of_trip(trip.id, day)
    items = await ChecklistRepository(ctx.scope, ctx.clock).of_trip(trip.id, day=day)
    return {
        "day": day,
        "currency": trip.currency,
        "activities": [_activity(activity, trip.currency) for activity in activities],
        "checklist": [_item(item) for item in items],
    }


@tool("read")
async def travel_activity(ctx: ActorContext, activity_id: ActivityId) -> dict[str, Any]:
    """One activity of a trip (an estimated cost in the trip's currency's main unit), with the
    checklist and packing items tied to it."""
    activity, trip = await Activities(ctx).with_trip(activity_id)
    ensure_ai(trip)
    items = await ChecklistRepository(ctx.scope, ctx.clock).tied_to(activity)
    return {
        "trip_id": str(trip.id),
        "currency": trip.currency,
        "activity": _activity(activity, trip.currency),
        "checklist": [_item(item) for item in items],
    }


@tool("read")
async def travel_budget(ctx: ActorContext, trip_id: TripId) -> dict[str, Any]:
    """A trip's budget in its currency's main unit: the budget, what is estimated and spent in
    all and by category, what is left, and how many costs have no rate to convert them."""
    await _trip(ctx, trip_id)
    summary = await Budget(ctx).summary(trip_id)
    figures = in_major(summary.model_dump(include=BUDGET_FIGURES), summary.currency)
    return dict(figures) | {"unconverted": len(summary.unconverted)}


@tool("client")
async def travel_trip_draft(ctx: ActorContext, trip: TravelTripDraft) -> None:
    """Fill the new-trip steps with the trip the person described, for them to check and save.
    Nothing is saved until they do."""
    check_dates(TripIn.model_validate(trip.as_input(ctx.user.base_currency)))


@tool("client")
async def travel_day_draft(
    ctx: ActorContext, trip_id: TripId, day: Day, activities: AiActivities
) -> None:
    """Show the person activities for one day of the trip, to add after the day's own. Nothing
    is added until they apply it."""
    trip = await _trip(ctx, trip_id)
    drafted = [activity.as_input(trip.currency) for activity in activities]
    draft = TravelDayDraftIn.model_validate({"day": day, "activities": drafted})
    await DayDrafts(ctx).run(trip_id, draft)


@tool("client")
async def travel_items_draft(
    ctx: ActorContext, trip_id: TripId, activity_id: ActivityId, items: DraftedItems
) -> None:
    """Show the person checklist and packing items for one activity, to add as the assistant's
    suggestions. Nothing is added until they apply it."""
    await _trip(ctx, trip_id)
    await AiItems(ctx).run(trip_id, TravelChecklistAssistIn(activity_id=activity_id, items=items))


@tool("client")
async def travel_change_draft(
    ctx: ActorContext, trip_id: TripId, activity_id: ActivityId, change: TravelChangeDraft
) -> None:
    """Show the person the change preview for one activity: done, skipped, replaced or
    rescheduled, as they said. Nothing changes until they apply it."""
    activity, trip = await Activities(ctx).with_trip(activity_id)
    if str(trip.id) != trip_id:
        raise UnknownActivityError
    ensure_ai(trip)
    checked = TravelChangeIn.model_validate(change.as_input(trip.currency))
    await Changes(ctx).run(str(activity.id), checked)


@tool("client")
async def travel_open(
    ctx: ActorContext,
    trip_id: TripId,
    section: Section = "overview",  # noqa: ARG001 - for the page, which opens it
    day: Annotated[Day | None, Field(description="For itinerary or prepare: that day")] = None,
) -> None:
    """Open one of the trip's pages for the person."""
    trip = await _trip(ctx, trip_id)
    if day:
        ensure_in_trip(trip, day)
