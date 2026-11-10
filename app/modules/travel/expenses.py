"""A trip's costs: what is estimated and what was really spent, each in the currency it was in (the
budget converts them: budget.py). Who paid and who share one are travellers of the trip, and the
activity it is for is one of the trip's (rule 6). Adding, editing and removing one each write an
event, so they can be undone from the history."""

from __future__ import annotations

from typing import Any

from app.modules.travel.checklist import UnknownActivityError
from app.modules.travel.events import (
    EXPENSE,
    SOURCE_MANUAL,
    Events,
    ExpenseStore,
    Saved,
    edit,
    removed,
)
from app.modules.travel.models import (
    ActivityRepository,
    ExpenseRepository,
    Trip,
    TripEvent,
    TripExpense,
    made_with,
)
from app.modules.travel.schemas import TravelExpenseIn, TravelExpensePatch
from app.modules.travel.trips import Trips, UnknownTravellerError
from app.platform import ActorContext, VersionConflictError, patched

ADDED = "expense_added"


class TripExpenses:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.trips = Trips(ctx)
        self.repository = ExpenseRepository(ctx.scope, ctx.clock)
        self.activities = ActivityRepository(ctx.scope, ctx.clock)
        self.events = Events(ctx)

    async def of_trip(self, trip_id: str) -> list[TripExpense]:
        trip = await self.trips.get(trip_id)
        return await self.repository.of_trip(trip.id)

    async def _with_trip(self, expense_id: str) -> tuple[TripExpense, Trip]:
        """The expense and its trip: a deleted trip's expenses are out of reach too."""
        expense = await self.repository.get(expense_id)
        return expense, await self.trips.get(expense.trip_id)

    async def _stored(
        self, trip: Trip, body: TravelExpenseIn, before: str | None = None
    ) -> dict[str, Any]:
        """The expense as stored, with whom and what it names checked against its trip. One the
        person kept may stay tied to an activity since removed (``before``)."""
        named = {body.paid_by, *body.shared_with} - {None}
        if not named <= {traveller.id for traveller in trip.travellers}:
            raise UnknownTravellerError
        if body.activity_id and body.activity_id != before:
            activity = await self.activities.find(body.activity_id)
            if activity is None or activity.trip_id != trip.id:
                raise UnknownActivityError
        return {**body.model_dump(mode="json"), "currency": body.currency or trip.currency}

    async def add(
        self, trip_id: str, body: TravelExpenseIn, key: str | None
    ) -> tuple[Saved[TripExpense], bool]:
        """The second value says it was made now, not by an earlier request with this key."""
        trip = await self.trips.get(trip_id)
        if earlier := await made_with(self.repository, key):
            return await self.events.made(earlier, ADDED, trip.id), False
        await ExpenseStore(self.ctx).ensure_room({"trip_id": trip.id})
        fields = {**await self._stored(trip, body), "trip_id": trip.id}
        expense, made = await self.repository.create_once(fields, key, source=SOURCE_MANUAL)
        if not made:
            return await self.events.made(expense, ADDED, trip.id), False
        event = await self.events.record_made(
            trip.id, ADDED, EXPENSE, expense, title=expense.title, day=expense.day
        )
        await self.trips.noted("expense_added", title=expense.title, trip=trip.title)
        return Saved(expense, event), True

    async def change(self, expense_id: str, body: TravelExpensePatch) -> Saved[TripExpense]:
        before, trip = await self._with_trip(expense_id)
        if body.revision_no != before.revision_no:
            raise VersionConflictError(details={"current": before.revision_no})
        checked = patched(TravelExpenseIn, before, body)
        fields = await self._stored(trip, checked, before.activity_id)
        planned = edit(EXPENSE, before, fields, title=before.title, day=before.day)
        event = await self.events.commit(
            before.trip_id,
            "expense_edited",
            [planned],
            summary={"title": before.title, "day": before.day},
        )
        return Saved(await self.repository.get(expense_id), event)

    async def delete(self, expense_id: str) -> TripEvent | None:
        expense = (await self._with_trip(expense_id))[0]
        return await self.events.commit(
            expense.trip_id,
            "expense_removed",
            [removed(EXPENSE, expense, title=expense.title, day=expense.day)],
            summary={"title": expense.title, "day": expense.day},
        )
