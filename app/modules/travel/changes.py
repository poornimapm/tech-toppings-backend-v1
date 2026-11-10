"""Changing one activity: done, skipped, replaced or rescheduled. Without ``apply`` the answer is
only the preview, and nothing at all is written; with it, what the effects planned is written and
recorded as one event, which can be undone. Only what depends on the activity is reassessed, so
another day's records never change."""

from __future__ import annotations

from typing import ClassVar

from app.modules.travel.activities import Activities
from app.modules.travel.events import Events, writing
from app.modules.travel.impact import (
    Effect,
    Impact,
    activity_effect,
    booking_effect,
    checklist_effect,
    expense_effect,
)
from app.modules.travel.models import ActivityStatus, Trip, TripActivity
from app.modules.travel.schemas import ChangeAction, TravelChangeIn, TravelChangeOut
from app.platform import ActorContext, ConflictError, VersionConflictError

# Every kind of record a change reaches, each said by one effect (impact.py).
EFFECTS: tuple[Effect, ...] = (activity_effect, checklist_effect, booking_effect, expense_effect)
KINDS = {
    ChangeAction.DONE: "activity_done",
    ChangeAction.SKIP: "activity_skipped",
    ChangeAction.REPLACE: "activity_replaced",
    ChangeAction.RESCHEDULE: "activity_rescheduled",
}


class ActivityStateError(ConflictError):
    code: ClassVar[str] = "TRAVEL_ACTIVITY_STATE"
    default_message = "Only a planned activity can change this way. Undo its last change first."


class Changes:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.activities = Activities(ctx)
        self.events = Events(ctx)

    async def _assess(self, trip: Trip, activity: TripActivity, body: TravelChangeIn) -> Impact:
        found = [await effect(self.ctx, trip, activity, body) for effect in EFFECTS]
        return Impact(
            planned=[item for impact in found for item in impact.planned],
            needs_review=[item for impact in found for item in impact.needs_review],
            kept=[item for impact in found for item in impact.kept],
        )

    async def run(self, activity_id: str, body: TravelChangeIn) -> TravelChangeOut:
        activity, trip = await self.activities.with_trip(activity_id)
        if body.revision_no not in (None, activity.revision_no):
            raise VersionConflictError(details={"current": activity.revision_no})
        if activity.status is not ActivityStatus.PLANNED:
            raise ActivityStateError(details={"status": activity.status})
        impact = await self._assess(trip, activity, body)
        if not body.apply:
            preview = [item.shown for item in writing(impact.planned)]
            return TravelChangeOut.of(preview, impact.needs_review, impact.kept)
        event = await self.events.commit(
            trip.id,
            KINDS[body.action],
            impact.planned,
            summary={
                "title": activity.title,
                "day": activity.day,
                "note": body.actual_note,
                "needs_review": len(impact.needs_review),
                "kept": len(impact.kept),
            },
        )
        return TravelChangeOut.of(
            event.changes if event else [],
            impact.needs_review,
            impact.kept,
            str(event.id) if event else None,
        )
