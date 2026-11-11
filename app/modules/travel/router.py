"""The routes, under ``/v1/travel``: every record is the signed-in person's own (anyone else's
trip, activity, booking, expense or event answers 404), and each route names one record. A write
answers the event of the trip's history it recorded (``event_id``), so the page undoes exactly
that one. AI help (``assist``) starts a background run and answers 202 at once; what it drafts is
applied here like anything else, after the person has seen the preview."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response, status

from app.modules.travel.activities import Activities
from app.modules.travel.assist import Assist
from app.modules.travel.bookings import Bookings
from app.modules.travel.budget import Budget
from app.modules.travel.changes import Changes
from app.modules.travel.checklist import Checklist
from app.modules.travel.drafts import AiItems, DayDrafts
from app.modules.travel.events import Events
from app.modules.travel.expenses import TripExpenses
from app.modules.travel.models import TripPhase
from app.modules.travel.overview import Overview
from app.modules.travel.prepare import Prepare
from app.modules.travel.rules import PackingBag, rules
from app.modules.travel.schemas import (
    TravelActivityIn,
    TravelActivityOut,
    TravelActivityPatch,
    TravelActivityRecorded,
    TravelAssistIn,
    TravelAssistOut,
    TravelBookingIn,
    TravelBookingOut,
    TravelBookingPatch,
    TravelBookingRecorded,
    TravelBudgetOut,
    TravelChangeIn,
    TravelChangeOut,
    TravelChecklistAssistIn,
    TravelChecklistIn,
    TravelChecklistOut,
    TravelChecklistPatch,
    TravelChecklistRecorded,
    TravelDayDraftIn,
    TravelDescribeIn,
    TravelEventOut,
    TravelExpenseIn,
    TravelExpenseOut,
    TravelExpensePatch,
    TravelExpenseRecorded,
    TravelOptions,
    TravelOverviewOut,
    TravelPrepareOut,
    TravelRecorded,
    TravelReorderIn,
    TravelSuggestIn,
    TravelSuggestOut,
    TravelTripOut,
    TravelTripRecorded,
    TripIn,
    TripPatch,
)
from app.modules.travel.suggestions import Suggestions
from app.modules.travel.trips import Trips, premium_allowed
from app.platform import (
    ActorDep,
    IdempotencyKey,
    Page,
    PageParamsDep,
    answer_once,
    error_responses,
    rate_limit,
)

router = APIRouter()


def trips(ctx: ActorDep) -> Trips:
    return Trips(ctx)


def activities(ctx: ActorDep) -> Activities:
    return Activities(ctx)


def checklist(ctx: ActorDep) -> Checklist:
    return Checklist(ctx)


def bookings(ctx: ActorDep) -> Bookings:
    return Bookings(ctx)


def expenses(ctx: ActorDep) -> TripExpenses:
    return TripExpenses(ctx)


TripsDep = Annotated[Trips, Depends(trips)]
ActivitiesDep = Annotated[Activities, Depends(activities)]
ChecklistDep = Annotated[Checklist, Depends(checklist)]
BookingsDep = Annotated[Bookings, Depends(bookings)]
ExpensesDep = Annotated[TripExpenses, Depends(expenses)]


@router.get(
    "/options", summary="What the forms offer and check", responses=error_responses(401, 403)
)
async def travel_options(ctx: ActorDep) -> TravelOptions:
    settings = rules()
    return TravelOptions(
        activity_kinds=list(settings.activities.kinds),
        max_trip_days=settings.trips.max_days,
        max_destinations=settings.trips.max_destinations,
        max_travellers=settings.trips.max_travellers,
        max_interests=settings.trips.max_interests,
        premium_available=premium_allowed(ctx),
        bags=list(PackingBag),
        max_item_quantity=settings.checklist.max_quantity,
        expense_categories=list(settings.budget.categories),
    )


@router.post(
    "/assist",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Describe a trip in your words: AI drafts the new-trip steps (describe-trip)",
    description="Starts a background AI run and answers its id at once. Its draft "
    "(`travel_trip_draft`) fills the planning steps; nothing is saved by it.",
    dependencies=[Depends(rate_limit("agent_run"))],
    responses=error_responses(401, 403, 422, 429),
)
async def describe_trip(body: TravelDescribeIn, ctx: ActorDep) -> TravelAssistOut:
    return await Assist(ctx).describe(body)


# ---------------------------------------------------------------------- trips


@router.post(
    "/trips",
    status_code=status.HTTP_201_CREATED,
    summary="Start a trip (once per Idempotency-Key)",
    responses=error_responses(401, 403, 409, 422),
)
async def add_trip(
    body: TripIn, service: TripsDep, response: Response, key: IdempotencyKey = None
) -> TravelTripRecorded:
    return service.recorded(answer_once(response, await service.add(body, key)))


@router.get(
    "/trips",
    summary="My trips: all, or upcoming, active or past",
    responses=error_responses(401, 403, 422),
)
async def list_trips(
    service: TripsDep, params: PageParamsDep, phase: TripPhase | None = None
) -> Page[TravelTripOut]:
    found, total = await service.page(params, phase)
    return Page[TravelTripOut].build([service.out(trip) for trip in found], params, total)


@router.get("/trips/{trip_id}", responses=error_responses(401, 403, 404))
async def read_trip(trip_id: str, service: TripsDep) -> TravelTripOut:
    return service.out(await service.get(trip_id))


@router.patch(
    "/trips/{trip_id}",
    summary="Change a trip (only what is sent), as of the revision edited",
    responses=error_responses(401, 403, 404, 409, 422),
)
async def change_trip(trip_id: str, body: TripPatch, service: TripsDep) -> TravelTripRecorded:
    return service.recorded(await service.change(trip_id, body))


@router.delete(
    "/trips/{trip_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a trip (it can be brought back)",
    responses=error_responses(401, 403, 404),
)
async def delete_trip(trip_id: str, service: TripsDep) -> None:
    await service.delete(trip_id)


@router.post(
    "/trips/{trip_id}/undelete",
    summary="Bring a deleted trip back, with everything in it, while there is room for it",
    responses=error_responses(401, 403, 404, 409),
)
async def undelete_trip(trip_id: str, service: TripsDep) -> TravelTripRecorded:
    return service.recorded(await service.undelete(trip_id))


@router.get(
    "/trips/{trip_id}/overview",
    summary="A trip at a glance: countdown, what's next and open, readiness and the next action",
    responses=error_responses(401, 403, 404),
)
async def trip_overview(trip_id: str, ctx: ActorDep) -> TravelOverviewOut:
    return await Overview(ctx).of(trip_id)


@router.post(
    "/trips/{trip_id}/assist",
    status_code=status.HTTP_202_ACCEPTED,
    summary="AI help on a Basic AI trip: draft a day, prepare an activity, say what changed, ask",
    description="Starts a background AI run and answers its id at once: `day` (plan-day) drafts "
    "`travel_day_draft`, `activity` (activity-checklist) `travel_items_draft`, `change` "
    "(update-trip) `travel_change_draft`, and `ask` (ask-trip) answers in words. A trip not in "
    "Basic answers 409 `TRAVEL_AI_OFF`. Nothing is written by a run.",
    dependencies=[Depends(rate_limit("agent_run"))],
    responses=error_responses(401, 403, 404, 409, 422, 429),
)
async def assist_trip(trip_id: str, body: TravelAssistIn, ctx: ActorDep) -> TravelAssistOut:
    return await Assist(ctx).on_trip(trip_id, body)


@router.get(
    "/trips/{trip_id}/activities",
    summary="A trip's activities in timeline order, or one day's",
    responses=error_responses(401, 403, 404, 422),
)
async def list_activities(
    trip_id: str,
    service: ActivitiesDep,
    day: Annotated[dt.date | None, Query(description="YYYY-MM-DD")] = None,
) -> list[TravelActivityOut]:
    return [TravelActivityOut.of(item) for item in await service.of_trip(trip_id, day)]


@router.post(
    "/trips/{trip_id}/activities",
    status_code=status.HTTP_201_CREATED,
    summary="Add an activity, last in its day (once per Idempotency-Key)",
    responses=error_responses(401, 403, 404, 409, 422),
)
async def add_activity(
    trip_id: str,
    body: TravelActivityIn,
    service: ActivitiesDep,
    response: Response,
    key: IdempotencyKey = None,
) -> TravelActivityRecorded:
    saved = answer_once(response, await service.add(trip_id, body, key))
    return TravelActivityRecorded.of(saved.record, event_id=saved.event_id)


@router.post(
    "/trips/{trip_id}/activities/reorder",
    summary="A new order for one day: every activity of that day, each once",
    responses=error_responses(401, 403, 404, 409, 422),
)
async def reorder_activities(
    trip_id: str, body: TravelReorderIn, service: ActivitiesDep
) -> list[TravelActivityRecorded]:
    """The day as it now stands, each activity with the reorder's event (none: nothing
    moved)."""
    saved = await service.reorder(trip_id, body)
    return [TravelActivityRecorded.of(item, event_id=saved.event_id) for item in saved.record]


@router.post(
    "/trips/{trip_id}/activities/draft",
    summary="Add a drafted day's activities after the day's own: a preview until apply is true",
    description="An AI run's draft (`ai_run_id`) is added once: again only after that addition "
    "is undone (409 `TRAVEL_AI_DRAFT_APPLIED`).",
    responses=error_responses(401, 403, 404, 409, 422),
)
async def draft_day(trip_id: str, body: TravelDayDraftIn, ctx: ActorDep) -> TravelChangeOut:
    return await DayDrafts(ctx).run(trip_id, body)


@router.get(
    "/trips/{trip_id}/checklist",
    summary="A trip's checklist and packing items, or those of a day, a bag or an activity",
    responses=error_responses(401, 403, 404, 422),
)
async def list_checklist(
    trip_id: str,
    service: ChecklistDep,
    day: Annotated[dt.date | None, Query(description="YYYY-MM-DD")] = None,
    bag: PackingBag | None = None,
    activity_id: Annotated[str | None, Query(max_length=32)] = None,
) -> list[TravelChecklistOut]:
    found = await service.of_trip(trip_id, day, bag, activity_id)
    return [TravelChecklistOut.of(item) for item in found]


@router.post(
    "/trips/{trip_id}/checklist",
    status_code=status.HTTP_201_CREATED,
    summary="Add a checklist or packing item (once per Idempotency-Key)",
    responses=error_responses(401, 403, 404, 409, 422),
)
async def add_checklist_item(
    trip_id: str,
    body: TravelChecklistIn,
    service: ChecklistDep,
    response: Response,
    key: IdempotencyKey = None,
) -> TravelChecklistRecorded:
    saved = answer_once(response, await service.add(trip_id, body, key))
    return TravelChecklistRecorded.of(saved.record, event_id=saved.event_id)


@router.post(
    "/trips/{trip_id}/checklist/suggest",
    summary="Suggest items from the trip's activities and length: a preview until apply is true",
    responses=error_responses(401, 403, 404, 409, 422),
)
async def suggest_checklist(trip_id: str, body: TravelSuggestIn, ctx: ActorDep) -> TravelSuggestOut:
    return await Suggestions(ctx).run(trip_id, body)


@router.post(
    "/trips/{trip_id}/checklist/assist",
    summary="Add an activity's AI items (the assistant's suggestions): a preview until apply is "
    "true",
    responses=error_responses(401, 403, 404, 409, 422),
)
async def assist_checklist(
    trip_id: str, body: TravelChecklistAssistIn, ctx: ActorDep
) -> TravelSuggestOut:
    return await AiItems(ctx).run(trip_id, body)


@router.get(
    "/trips/{trip_id}/bookings",
    summary="A trip's bookings: those with no time first, then as they start",
    responses=error_responses(401, 403, 404),
)
async def list_bookings(trip_id: str, service: BookingsDep) -> list[TravelBookingOut]:
    return [TravelBookingOut.of(item) for item in await service.of_trip(trip_id)]


@router.post(
    "/trips/{trip_id}/bookings",
    status_code=status.HTTP_201_CREATED,
    summary="Add a booking as I have it (once per Idempotency-Key)",
    responses=error_responses(401, 403, 404, 409, 422),
)
async def add_booking(
    trip_id: str,
    body: TravelBookingIn,
    service: BookingsDep,
    response: Response,
    key: IdempotencyKey = None,
) -> TravelBookingRecorded:
    saved = answer_once(response, await service.add(trip_id, body, key))
    return TravelBookingRecorded.of(saved.record, event_id=saved.event_id)


@router.get(
    "/trips/{trip_id}/expenses",
    summary="A trip's estimates and actual costs, by day",
    responses=error_responses(401, 403, 404),
)
async def list_expenses(trip_id: str, service: ExpensesDep) -> list[TravelExpenseOut]:
    return [TravelExpenseOut.of(item) for item in await service.of_trip(trip_id)]


@router.post(
    "/trips/{trip_id}/expenses",
    status_code=status.HTTP_201_CREATED,
    summary="Add an estimate or an actual cost (once per Idempotency-Key)",
    responses=error_responses(401, 403, 404, 409, 422),
)
async def add_expense(
    trip_id: str,
    body: TravelExpenseIn,
    service: ExpensesDep,
    response: Response,
    key: IdempotencyKey = None,
) -> TravelExpenseRecorded:
    saved = answer_once(response, await service.add(trip_id, body, key))
    return TravelExpenseRecorded.of(saved.record, event_id=saved.event_id)


@router.get(
    "/trips/{trip_id}/budget",
    summary="Estimated against actual by category in the trip's currency, and who owes whom",
    responses=error_responses(401, 403, 404),
)
async def trip_budget(trip_id: str, ctx: ActorDep) -> TravelBudgetOut:
    return await Budget(ctx).summary(trip_id)


@router.get(
    "/trips/{trip_id}/prepare",
    summary="One day's plan: its activities, checklist, bookings and transport",
    responses=error_responses(401, 403, 404, 422),
)
async def prepare_day(
    trip_id: str,
    ctx: ActorDep,
    when: Annotated[
        Literal["today", "tomorrow"] | dt.date,
        Query(description="today or tomorrow on the trip's own calendar, or YYYY-MM-DD"),
    ] = "today",
) -> TravelPrepareOut:
    return await Prepare(ctx).day(trip_id, when)


@router.get(
    "/trips/{trip_id}/events",
    summary="What changed on a trip, newest first",
    responses=error_responses(401, 403, 404, 422),
)
async def list_events(trip_id: str, ctx: ActorDep, params: PageParamsDep) -> Page[TravelEventOut]:
    found, total = await Events(ctx).page(trip_id, params)
    return Page[TravelEventOut].build([TravelEventOut.of(item) for item in found], params, total)


# ---------------------------------------------------------------------- activities


@router.get("/activities/{activity_id}", responses=error_responses(401, 403, 404))
async def read_activity(activity_id: str, service: ActivitiesDep) -> TravelActivityOut:
    return TravelActivityOut.of(await service.get(activity_id))


@router.patch(
    "/activities/{activity_id}",
    summary="Edit an activity (only what is sent), as of the revision edited",
    responses=error_responses(401, 403, 404, 409, 422),
)
async def change_activity(
    activity_id: str, body: TravelActivityPatch, service: ActivitiesDep
) -> TravelActivityRecorded:
    saved = await service.change(activity_id, body)
    return TravelActivityRecorded.of(saved.record, event_id=saved.event_id)


@router.delete(
    "/activities/{activity_id}",
    summary="Remove an activity (undone from the trip's history, by the event answered)",
    responses=error_responses(401, 403, 404, 409),
)
async def delete_activity(activity_id: str, service: ActivitiesDep) -> TravelRecorded:
    return TravelRecorded.of_event(await service.delete(activity_id))


@router.post(
    "/activities/{activity_id}/change",
    summary="Mark it done, skip, replace or reschedule it: a preview until apply is true",
    responses=error_responses(401, 403, 404, 409, 422),
)
async def apply_change(activity_id: str, body: TravelChangeIn, ctx: ActorDep) -> TravelChangeOut:
    return await Changes(ctx).run(activity_id, body)


# ---------------------------------------------------------------------- checklist items


@router.patch(
    "/checklist/{item_id}",
    summary="Change or tick an item (only what is sent)",
    responses=error_responses(401, 403, 404, 409, 422),
)
async def change_checklist_item(
    item_id: str, body: TravelChecklistPatch, service: ChecklistDep
) -> TravelChecklistRecorded:
    saved = await service.change(item_id, body)
    return TravelChecklistRecorded.of(saved.record, event_id=saved.event_id)


@router.delete(
    "/checklist/{item_id}",
    summary="Remove an item: a suggestion removed isn't suggested again",
    responses=error_responses(401, 403, 404, 409),
)
async def delete_checklist_item(item_id: str, service: ChecklistDep) -> TravelRecorded:
    return TravelRecorded.of_event(await service.delete(item_id))


# ---------------------------------------------------------------------- bookings


@router.get("/bookings/{booking_id}", responses=error_responses(401, 403, 404))
async def read_booking(booking_id: str, service: BookingsDep) -> TravelBookingOut:
    return TravelBookingOut.of(await service.get(booking_id))


@router.patch(
    "/bookings/{booking_id}",
    summary="Edit a booking (only what is sent), as of the revision edited",
    responses=error_responses(401, 403, 404, 409, 422),
)
async def change_booking(
    booking_id: str, body: TravelBookingPatch, service: BookingsDep
) -> TravelBookingRecorded:
    saved = await service.change(booking_id, body)
    return TravelBookingRecorded.of(saved.record, event_id=saved.event_id)


@router.delete(
    "/bookings/{booking_id}",
    summary="Remove a booking no activity links to (undone from the trip's history)",
    responses=error_responses(401, 403, 404, 409),
)
async def delete_booking(booking_id: str, service: BookingsDep) -> TravelRecorded:
    return TravelRecorded.of_event(await service.delete(booking_id))


# ---------------------------------------------------------------------- expenses


@router.patch(
    "/expenses/{expense_id}",
    summary="Edit an expense (only what is sent), as of the revision edited",
    responses=error_responses(401, 403, 404, 409, 422),
)
async def change_expense(
    expense_id: str, body: TravelExpensePatch, service: ExpensesDep
) -> TravelExpenseRecorded:
    saved = await service.change(expense_id, body)
    return TravelExpenseRecorded.of(saved.record, event_id=saved.event_id)


@router.delete(
    "/expenses/{expense_id}",
    summary="Remove an expense (undone from the trip's history)",
    responses=error_responses(401, 403, 404, 409),
)
async def delete_expense(expense_id: str, service: ExpensesDep) -> TravelRecorded:
    return TravelRecorded.of_event(await service.delete(expense_id))


# ---------------------------------------------------------------------- history


@router.post(
    "/events/{event_id}/undo",
    summary="Take one change back, if nothing it touched has changed since",
    responses=error_responses(401, 403, 404, 409, 422),
)
async def undo_event(event_id: str, ctx: ActorDep) -> TravelEventOut:
    return TravelEventOut.of(await Events(ctx).undo(event_id))
