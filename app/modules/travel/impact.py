"""What changing an activity reaches. An effect looks at one kind of record and says what the change
would do to it, writing nothing: what it would write (``planned``), what the person should look at
(``needs_review``, never changed) and what it leaves alone because it is their own work (``kept``).
Everything the change writes it writes through a step, so a preview, the change and its undo all
agree. ``changes.EFFECTS`` lists the effects: another kind of record joins by adding one there."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.modules.travel.activities import Activities, stand_down
from app.modules.travel.bookings import check_booking
from app.modules.travel.events import (
    ACTIVITY,
    BOOKING,
    CHECKLIST,
    EXPENSE,
    Planned,
    added,
    edit,
    ensure_in_trip,
)
from app.modules.travel.models import (
    ActivityStatus,
    BookingRepository,
    ChecklistRepository,
    ExpenseKind,
    ExpenseRepository,
    Trip,
    TripActivity,
)
from app.modules.travel.schemas import ChangeAction, TravelChangeIn, TravelFlag
from app.platform import ActorContext

STATUS_AFTER = {
    ChangeAction.DONE: ActivityStatus.DONE,
    ChangeAction.SKIP: ActivityStatus.SKIPPED,
    ChangeAction.REPLACE: ActivityStatus.REPLACED,
}
AWAY = (ChangeAction.SKIP, ChangeAction.REPLACE)  # it won't happen as planned
OFF_ITS_TIME = (*AWAY, ChangeAction.RESCHEDULE)  # not then, or not at all
# Reasons listed in needs_review: an estimate that may no longer stand, a booking for it.
ESTIMATE, LINKED = "estimate", "linked"


@dataclass
class Impact:
    planned: list[Planned] = field(default_factory=list)
    needs_review: list[TravelFlag] = field(default_factory=list)
    kept: list[TravelFlag] = field(default_factory=list)


Effect = Callable[[ActorContext, Trip, TripActivity, TravelChangeIn], Awaitable[Impact]]


async def _moved(
    service: Activities, trip: Trip, activity: TripActivity, body: TravelChangeIn
) -> dict[str, Any]:
    """A rescheduled activity's fields: last on a new day, and its place kept on the same one."""
    day = body.new_day.isoformat() if body.new_day else activity.day
    ensure_in_trip(trip, day)
    fields: dict[str, Any] = {"day": day}
    if body.new_start_time:
        fields["start_time"] = body.new_start_time
    if day != activity.day:
        fields["position"] = await service.repository.next_position(activity.trip_id, day)
    return fields


async def _replacement(
    service: Activities, activity: TripActivity, body: TravelChangeIn
) -> list[Planned]:
    """What takes its place: on its day, right after it. No other activity has to move for it."""
    new = body.new_activity
    if body.action is not ChangeAction.REPLACE or new is None:
        return []
    await check_booking(service.ctx, activity.trip_id, new.booking_id)
    await service.ensure_room(activity.trip_id)
    fields = {
        **new.model_dump(mode="json"),
        "trip_id": activity.trip_id,
        "day": activity.day,
        "position": activity.position,
    }
    return [added(ACTIVITY, fields, title=new.title, day=activity.day)]


async def activity_effect(
    ctx: ActorContext, trip: Trip, activity: TripActivity, body: TravelChangeIn
) -> Impact:
    """The activity itself, and what replaces it. An estimate it carries is left for the person
    to review once the activity won't happen as planned."""
    service = Activities(ctx)
    if body.action is ChangeAction.RESCHEDULE:
        fields = await _moved(service, trip, activity, body)
    else:
        fields = {"status": STATUS_AFTER[body.action].value}
        if body.actual_note:
            fields["actual_note"] = body.actual_note
    own = edit(ACTIVITY, activity, fields, title=activity.title, day=activity.day)
    impact = Impact([own, *await _replacement(service, activity, body)])
    if body.action in AWAY and activity.estimated_cost_minor is not None:
        impact.needs_review.append(
            TravelFlag(
                entity=ACTIVITY, record_id=str(activity.id), title=activity.title, reason=ESTIMATE
            )
        )
    return impact


async def checklist_effect(
    ctx: ActorContext, _trip: Trip, activity: TripActivity, body: TravelChangeIn
) -> Impact:
    """The items tied to the activity. A reschedule takes them all along, the person's own work
    too, to stay on its day; going away, they stand down. Marking it done leaves them be."""
    tied = await ChecklistRepository(ctx.scope, ctx.clock).tied_to(activity)
    if body.action is ChangeAction.RESCHEDULE and body.new_day:
        day = body.new_day.isoformat()
        return Impact(
            [
                edit(
                    CHECKLIST,
                    item,
                    {"day": day},
                    title=item.title,
                    day=item.day,
                    key=item.worded_by,
                )
                for item in tied
            ]
        )
    if body.action in AWAY:
        planned, kept = stand_down(tied)
        return Impact(planned, kept=kept)
    return Impact()


async def booking_effect(
    ctx: ActorContext, _trip: Trip, activity: TripActivity, body: TravelChangeIn
) -> Impact:
    """The booking the activity links to, once it won't happen then or at all: for the person to
    change or cancel with whoever it is with. Nothing here can, so it is listed and left be."""
    if body.action not in OFF_ITS_TIME or activity.booking_id is None:
        return Impact()
    booking = await BookingRepository(ctx.scope, ctx.clock).find(activity.booking_id)
    if booking is None:
        return Impact()
    flag = TravelFlag(entity=BOOKING, record_id=str(booking.id), title=booking.title, reason=LINKED)
    return Impact(needs_review=[flag])


async def expense_effect(
    ctx: ActorContext, _trip: Trip, activity: TripActivity, body: TravelChangeIn
) -> Impact:
    """The estimates tied to the activity, once it won't happen as planned: listed, and left as
    they are. What was really spent was spent, so it isn't."""
    if body.action not in AWAY:
        return Impact()
    estimates = await ExpenseRepository(ctx.scope, ctx.clock).of_trip(
        activity.trip_id, activity_id=str(activity.id), kind=ExpenseKind.ESTIMATE
    )
    return Impact(
        needs_review=[
            TravelFlag(entity=EXPENSE, record_id=str(item.id), title=item.title, reason=ESTIMATE)
            for item in estimates
        ]
    )
