"""A trip at a glance: how long until it starts or which day of it this is, what is next on the
plan, what is still open, how ready it is and what to do next, all on the trip's own calendar.

Readiness is counted from the checklist alone (rule 1): the items completed over those that still
apply. With none that apply there is no figure at all, never a made-up one. The next action is the
first rule that applies, in the order data/travel.yaml lists them (rule 2), and it says why with
figures from the trip's own records: the same records always give the same answer."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.modules.travel.models import (
    OPEN_STATES,
    ActivityRepository,
    BookingRepository,
    BookingStatus,
    ChecklistRepository,
    ChecklistState,
    Trip,
    TripPhase,
    trip_today,
)
from app.modules.travel.rules import NextAction, rules
from app.modules.travel.schemas import (
    TravelActivityOut,
    TravelNextAction,
    TravelOverviewOut,
    TravelPendingOut,
    TravelReadiness,
)
from app.modules.travel.trips import Trips, phase_on
from app.platform import ActorContext

TO_CONFIRM = (BookingStatus.ENTERED, BookingStatus.EXTRACTED)  # nobody has checked them yet
APPLYING = tuple(state for state in ChecklistState if state is not ChecklistState.NOT_APPLICABLE)
Tally = dict[tuple[ChecklistState, bool], int]  # items by state, and whether they are packing


@dataclass(frozen=True)
class TripState:
    """What the next-action rules look at."""

    phase: TripPhase
    end_date: str
    today: str
    tomorrow: str | None  # only while tomorrow is one of the trip's days
    activities: int
    open_today: int
    open_tomorrow: int
    bookings_to_confirm: int
    packing: int  # packing items that apply, done or not
    packing_open: int
    checklist_open: int
    tasks: int  # every item that applies, packing or not, done or not
    checklist_open_day: str | None = None  # the first day an open checklist item is on

    @property
    def ahead(self) -> bool:
        return self.phase is not TripPhase.PAST


Figures = dict[str, Any] | None  # what the reason is worded with; None: the rule doesn't apply


def _trip_ended(state: TripState) -> Figures:
    return {"end_date": state.end_date} if state.phase is TripPhase.PAST else None


def _add_activities(state: TripState) -> Figures:
    return {} if state.ahead and not state.activities else None


def _prepare_today(state: TripState) -> Figures:
    if state.phase is not TripPhase.ACTIVE or not state.open_today:
        return None
    return {"day": state.today, "open": state.open_today}


def _prepare_tomorrow(state: TripState) -> Figures:
    if state.tomorrow is None or not state.open_tomorrow:
        return None
    return {"day": state.tomorrow, "open": state.open_tomorrow}


def _confirm_bookings(state: TripState) -> Figures:
    return (
        {"count": state.bookings_to_confirm} if state.ahead and state.bookings_to_confirm else None
    )


def _finish_packing(state: TripState) -> Figures:
    return {"open": state.packing_open} if state.ahead and state.packing_open else None


def _start_packing(state: TripState) -> Figures:
    return {} if state.phase is TripPhase.UPCOMING and not state.packing else None


def _finish_checklist(state: TripState) -> Figures:
    """With the day to open for it: the first holding an open item, none when only the whole
    trip's are."""
    if not state.ahead or not state.checklist_open:
        return None
    return {"open": state.checklist_open, "day": state.checklist_open_day}


def _add_tasks(state: TripState) -> Figures:
    """Nothing to do or pack at all: that isn't being ready, only having no tasks yet."""
    return {} if state.ahead and not state.tasks else None


def _all_set(_state: TripState) -> Figures:
    return {}


# Each next action: the reason it gives, and the rule that says whether it applies.
RULES: dict[NextAction, tuple[str, Callable[[TripState], Figures]]] = {
    NextAction.TRIP_ENDED: ("trip_over", _trip_ended),
    NextAction.ADD_ACTIVITIES: ("no_activities", _add_activities),
    NextAction.PREPARE_TODAY: ("open_today", _prepare_today),
    NextAction.PREPARE_TOMORROW: ("open_tomorrow", _prepare_tomorrow),
    NextAction.CONFIRM_BOOKINGS: ("bookings_unconfirmed", _confirm_bookings),
    NextAction.FINISH_PACKING: ("packing_open", _finish_packing),
    NextAction.START_PACKING: ("no_packing", _start_packing),
    NextAction.FINISH_CHECKLIST: ("checklist_open", _finish_checklist),
    NextAction.ADD_TASKS: ("no_tasks", _add_tasks),
    NextAction.ALL_SET: ("nothing_open", _all_set),
}


def _count(tally: Tally, states: tuple[ChecklistState, ...], packing: bool | None = None) -> int:
    """Items in these states: packing ones, the others, or (``None``) all of them."""
    return sum(n for (state, bag), n in tally.items() if state in states and packing in (None, bag))


def readiness(tally: Tally) -> TravelReadiness | None:
    """Completed over applicable; no figure with nothing that applies."""
    applicable = _count(tally, APPLYING)
    if not applicable:
        return None
    completed = _count(tally, (ChecklistState.COMPLETED,))
    return TravelReadiness(completed=completed, applicable=applicable, ratio=completed / applicable)


def next_action(state: TripState) -> TravelNextAction:
    """The first rule that applies. ``all_set``, last, always does (rules.py checks it is last)."""
    for action in rules().overview.next_actions:
        reason, rule = RULES[action]
        params = rule(state)
        if params is not None:
            return TravelNextAction(action=action, reason=reason, params=params)
    return TravelNextAction(
        action=NextAction.ALL_SET, reason=RULES[NextAction.ALL_SET][0], params={}
    )


class Overview:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.trips = Trips(ctx)
        self.activities = ActivityRepository(ctx.scope, ctx.clock)
        self.checklist = ChecklistRepository(ctx.scope, ctx.clock)
        self.bookings = BookingRepository(ctx.scope, ctx.clock)

    async def _state(self, trip: Trip, today: dt.date, tally: Tally) -> TripState:
        day, after = today.isoformat(), (today + dt.timedelta(days=1)).isoformat()
        tomorrow = after if trip.start_date <= after <= trip.end_date else None
        to_confirm = {"status": {"$in": list(TO_CONFIRM)}}
        open_days = await self.checklist.open_days(trip.id, packing=False)
        return TripState(
            phase=phase_on(trip, today),
            end_date=trip.end_date,
            today=day,
            tomorrow=tomorrow,
            activities=await self.activities.count_of(trip.id),
            open_today=await self.checklist.open_on(trip.id, day),
            open_tomorrow=await self.checklist.open_on(trip.id, tomorrow) if tomorrow else 0,
            bookings_to_confirm=await self.bookings.count_of(trip.id, to_confirm),
            packing=_count(tally, APPLYING, packing=True),
            packing_open=_count(tally, OPEN_STATES, packing=True),
            checklist_open=_count(tally, OPEN_STATES, packing=False),
            tasks=_count(tally, APPLYING),
            checklist_open_day=open_days[0] if open_days else None,
        )

    async def of(self, trip_id: str) -> TravelOverviewOut:
        trip = await self.trips.get(trip_id)
        today = trip_today(trip, self.ctx)
        start, end = dt.date.fromisoformat(trip.start_date), dt.date.fromisoformat(trip.end_date)
        tally = await self.checklist.tally(trip.id)
        state = await self._state(trip, today, tally)
        ahead = await self.activities.ahead(
            trip.id, today.isoformat(), rules().overview.next_activities
        )
        return TravelOverviewOut(
            trip_id=str(trip.id),
            today=today,
            phase=state.phase,
            days_to_start=(start - today).days,
            day_number=(today - start).days + 1 if state.phase is TripPhase.ACTIVE else None,
            trip_days=(end - start).days + 1,
            next_activities=[TravelActivityOut.of(activity) for activity in ahead],
            pending=TravelPendingOut(
                checklist=state.checklist_open,
                packing=state.packing_open,
                bookings=state.bookings_to_confirm,
            ),
            readiness=readiness(tally),
            next_action=next_action(state),
        )
