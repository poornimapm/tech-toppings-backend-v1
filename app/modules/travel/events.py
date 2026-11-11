"""The trip's history, and how a change is written so that it can be taken back.

A change is planned as ``Step``s, one write to one record each. A step is checked first, which
answers the step that takes it back, and that is kept before the step is written: so a write that
lands and then fails (a read after it, say) is taken back as well. The write then says the version
it left, so the event keeps exactly what its undo needs. A store says how steps are run on one kind
of record (``STORES``): a new kind joins by adding its store there. There are no transactions
(ADR-0002), so a step that fails partway, for any reason, has itself and the ones before it taken
back, each on its own: one that can't be (the database gone, say) doesn't stop the rest. An event
is written once its change is saved, so a change taken back leaves none; whatever of it couldn't be
taken back is recorded all the same, so the history shows it and can offer to take it back later.
What follows a write (a trip's reminder) runs only once its change is recorded: failing, it leaves
the change saved and in the history, to be undone like any other.

An undo reverses one event while nothing it touched has changed since (each record is still at
the version the event left it at). So only the latest change to a record can be undone: an undo
moves the record on too. An undo is an event itself, and can't be undone.

A kind of record has a cap (data/travel.yaml), held by its store: on each write that makes one or
brings one back, an undo's too, so a list read whole never leaves one out."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Generic, Protocol, TypeVar

from beanie import PydanticObjectId
from fastapi import status
from pymongo.errors import DuplicateKeyError

from app.modules.travel.models import (
    NEWEST_EVENT,
    SOURCE_UNDO,
    ActivityRepository,
    BookingRepository,
    ChecklistItem,
    ChecklistRepository,
    EventRepository,
    EventSource,
    ExpenseRepository,
    RecordChange,
    Step,
    StepOp,
    Trip,
    TripActivity,
    TripEvent,
    TripExpense,
    TripRepository,
)
from app.modules.travel.reminder import sync_reminder
from app.modules.travel.rules import rules
from app.platform import (
    ActorContext,
    AppError,
    ConflictError,
    FieldChange,
    PageParams,
    RevisionedDocument,
    RevisionedRepository,
    VersionConflictError,
    get_logger,
)

logger = get_logger(__name__)
T = TypeVar("T")
Made = TypeVar("Made", bound=RevisionedDocument)

TRIP = "trip"
ACTIVITY = "activity"
CHECKLIST = "checklist"
BOOKING = "booking"
EXPENSE = "expense"
SOURCE_MANUAL = "manual"
UNDO_KIND = "undo"
# What a record's line says once its change is taken back.
OPPOSITE = {
    StepOp.SET: StepOp.SET,
    StepOp.ADD: StepOp.REMOVE,
    StepOp.REMOVE: StepOp.REVIVE,
    StepOp.REVIVE: StepOp.REMOVE,
}


class DayOutsideTripError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code: ClassVar[str] = "TRAVEL_DAY_OUTSIDE_TRIP"
    default_message = "That day isn't within the trip's dates."


class TripsFullError(ConflictError):
    code: ClassVar[str] = "TRAVEL_TRIPS_FULL"
    default_message = "That's as many trips as one person keeps: delete an old one first."


class ActivitiesFullError(ConflictError):
    code: ClassVar[str] = "TRAVEL_ACTIVITIES_FULL"
    default_message = "That's as many activities as one trip keeps."


class ChecklistFullError(ConflictError):
    code: ClassVar[str] = "TRAVEL_CHECKLIST_FULL"
    default_message = "That's as many items as one trip keeps."


class BookingsFullError(ConflictError):
    code: ClassVar[str] = "TRAVEL_BOOKINGS_FULL"
    default_message = "That's as many bookings as one trip keeps."


class ExpensesFullError(ConflictError):
    code: ClassVar[str] = "TRAVEL_EXPENSES_FULL"
    default_message = "That's as many expenses as one trip keeps."


class ActivitiesOutsideDatesError(ConflictError):
    code: ClassVar[str] = "TRAVEL_ACTIVITIES_OUTSIDE_DATES"
    default_message = "Some activities fall outside those dates: move or remove them first."


class ChecklistOutsideDatesError(ConflictError):
    code: ClassVar[str] = "TRAVEL_CHECKLIST_OUTSIDE_DATES"
    default_message = "Some checklist items fall outside those dates: move or remove them first."


class TravellerInUseError(ConflictError):
    code: ClassVar[str] = "TRAVEL_TRAVELLER_IN_USE"
    default_message = (
        "That traveller still has checklist items or expenses: reassign or remove them first."
    )


class SuggestionTakenError(ConflictError):
    code: ClassVar[str] = "TRAVEL_SUGGESTION_TAKEN"
    default_message = "These suggestions were just added elsewhere. Reload and try again."


class UndoUnavailableError(ConflictError):
    code: ClassVar[str] = "TRAVEL_UNDO_UNAVAILABLE"
    default_message = "This one can't be undone."


class UndoDoneError(ConflictError):
    code: ClassVar[str] = "TRAVEL_UNDO_DONE"
    default_message = "This was already undone."


class UndoStaleError(ConflictError):
    code: ClassVar[str] = "TRAVEL_UNDO_STALE"
    default_message = "Something it touched has changed since, so it can't be undone."


def ensure_in_trip(trip: Trip, day: str) -> None:
    if not trip.start_date <= day <= trip.end_date:
        raise DayOutsideTripError(
            details={"day": day, "start_date": trip.start_date, "end_date": trip.end_date}
        )


# ---------------------------------------------------------------------- planning a change


@dataclass(frozen=True)
class Planned:
    """One record's part in a change: what the page shows, and the write that does it."""

    shown: RecordChange
    step: Step


def edit(
    entity: str,
    record: RevisionedDocument,
    fields: Mapping[str, Any],
    *,
    title: str,
    day: str | None = None,
    key: str | None = None,
) -> Planned:
    """These fields of this record: those that differ. ``fields`` hold JSON values. ``key``: the
    suggestion the app words its title by (``ChecklistItem.worded_by``)."""
    now = record.model_dump(mode="json")
    differ = {name: value for name, value in fields.items() if now.get(name) != value}
    lines = [FieldChange(field=name, old=now.get(name), new=new) for name, new in differ.items()]
    record_id = str(record.id)
    shown = RecordChange(
        entity=entity,
        op=StepOp.SET,
        record_id=record_id,
        title=title,
        day=day,
        fields=lines,
        suggestion_key=key,
    )
    step = Step(
        entity=entity,
        op=StepOp.SET,
        record_id=record_id,
        fields=differ,
        expected=record.revision_no,
    )
    return Planned(shown, step)


def added(
    entity: str,
    fields: Mapping[str, Any],
    *,
    title: str,
    day: str | None = None,
    show: Sequence[str] = (),
    key: str | None = None,
) -> Planned:
    """A new record of these fields. The page is told the title and day, and those in ``show``
    that have a value."""
    lines = [FieldChange(field=name, new=fields[name]) for name in show if fields[name] is not None]
    return Planned(
        RecordChange(
            entity=entity, op=StepOp.ADD, title=title, day=day, fields=lines, suggestion_key=key
        ),
        Step(entity=entity, op=StepOp.ADD, fields=dict(fields)),
    )


def removed(
    entity: str,
    record: RevisionedDocument,
    *,
    title: str,
    day: str | None = None,
    key: str | None = None,
) -> Planned:
    """This record, soft-deleted."""
    record_id = str(record.id)
    shown = RecordChange(
        entity=entity,
        op=StepOp.REMOVE,
        record_id=record_id,
        title=title,
        day=day,
        suggestion_key=key,
    )
    step = Step(entity=entity, op=StepOp.REMOVE, record_id=record_id, expected=record.revision_no)
    return Planned(shown, step)


def writing(planned: Sequence[Planned]) -> list[Planned]:
    """Those that really write: an edit that differs in nothing doesn't."""
    return [item for item in planned if item.step.op is not StepOp.SET or item.step.fields]


# ---------------------------------------------------------------------- stores


class Store(Protocol):
    """How steps are run on one kind of record."""

    async def stale(self, step: Step) -> bool:
        """Whether the record has moved on from the version the step expects."""
        ...

    async def ready(self, step: Step) -> Step:
        """Check the step, or refuse it with an ``AppError``, writing nothing. Answers the step
        that takes it back once written, kept before the write: one that lands and then fails is
        taken back too."""
        ...

    async def write(self, step: Step, back: Step, source: str) -> Step:
        """Do the step ``ready`` answered ``back`` for. Answers ``back`` as of the version the
        write left."""
        ...

    async def revert(self, step: Step, back: Step) -> int | None:
        """Take ``step`` back by ``back``, the change it was part of having failed: whether its
        write landed or not. Answers the revision the record is at if it is now just as before
        the step."""
        ...

    async def follow(self, record_id: str) -> None:
        """What follows a write to the record (a trip's reminder), once its change is recorded."""
        ...


@dataclass(frozen=True)
class Room:
    """How many live records of a kind one trip keeps (data/travel.yaml), and what answers one
    more."""

    most: Callable[[], int]
    full: type[ConflictError]


class RevisionedStore:
    """Steps on a revisioned record, whose version is its ``revision_no``."""

    def __init__(self, repository: RevisionedRepository[Any], room: Room | None = None) -> None:
        self.repository = repository
        self.room = room

    async def check(self, record: Mapping[str, Any]) -> None:
        """Refuse a record that wouldn't fit, given all its fields as the step would leave them."""

    async def ensure_room(self, record: Mapping[str, Any]) -> None:
        """Refuse one more live record of this kind on the trip ``record`` is of, past its cap."""
        if self.room is None:
            return
        on_trip = self.repository.query({"trip_id": PydanticObjectId(record["trip_id"])})
        if await self.repository.model.find(on_trip).count() >= self.room.most():
            raise self.room.full

    async def stale(self, step: Step) -> bool:
        found = await self.repository.find(step.record_id or "", include_deleted=True)
        return found is None or found.revision_no != step.expected

    async def ready(self, step: Step) -> Step:
        """A record to make gets its id now, so what removes it again is known before it is
        made. Each write moves a record on one version."""
        if step.op is StepOp.ADD:
            await self.ensure_room(step.fields)
            await self.check(step.fields)
            return _back(step, StepOp.REMOVE, str(PydanticObjectId()), 1)
        record_id = step.record_id or ""
        found = await self.repository.get(record_id, include_deleted=True)
        if found.revision_no != step.expected:
            raise VersionConflictError(details={"current": found.revision_no})
        following = found.revision_no + 1
        if step.op is StepOp.REMOVE:
            return _back(step, StepOp.REVIVE, record_id, following)
        record = {**found.model_dump(), **step.fields}
        gone = found.deleted_at is not None
        if step.op is StepOp.REVIVE and gone:
            await self.ensure_room(record)
        await self.check(record)
        if step.op is StepOp.REVIVE:
            return _back(step, StepOp.REMOVE, record_id, following if gone else found.revision_no)
        was = found.model_dump(mode="json")
        before = {name: was.get(name) for name in step.fields}
        return _back(step, StepOp.SET, record_id, following, before)

    async def write(self, step: Step, back: Step, source: str) -> Step:
        record_id = back.record_id or ""
        if step.op is StepOp.ADD:
            fields = {**step.fields, "id": PydanticObjectId(record_id)}
            made = await self.repository.create(fields, source=source)
            return back.model_copy(update={"expected": made.revision_no})
        if step.op is StepOp.REMOVE:
            await self.repository.soft_delete(record_id, source=source)
            return back
        if step.op is StepOp.REVIVE:
            revived = await self.repository.undelete(record_id, source=source)
            return back.model_copy(update={"expected": revived.revision_no})
        changed = await self.repository.update(
            record_id, step.fields, expected=step.expected, source=source
        )
        return back.model_copy(update={"expected": changed.revision_no})

    async def run(self, step: Step, source: str) -> Step:
        """A step on its own, outside any change (a tick): checked, then written."""
        return await self.write(step, await self.ready(step), source)

    async def revert(self, step: Step, back: Step) -> int | None:
        """Nothing is checked, as all goes back to how it was before; but what someone has
        written since stays theirs. A record made is removed again, one removed comes back, and a
        field is set back only while it still holds what ``step`` wrote: a write that never
        landed leaves nothing to do. Answers the revision it is at now if nobody had written to
        it since, so it is just as before the step."""
        found = await self.repository.find(back.record_id or "", include_deleted=True)
        if found is None:  # never made
            return None
        revision = await self._write_back(step, back, found)
        return revision if found.revision_no == back.expected else None

    async def _write_back(self, step: Step, back: Step, found: RevisionedDocument) -> int | None:
        """``back`` written over ``found`` as far as it is still the step's: the revision it
        leaves, or None with nothing to write."""
        record_id = back.record_id or ""
        gone = found.deleted_at is not None
        if back.op is StepOp.REMOVE:
            if gone:
                return None
            await self.repository.soft_delete(record_id, source=SOURCE_UNDO)
            return found.revision_no + 1
        if back.op is StepOp.REVIVE:
            if not gone:
                return None
            revived: RevisionedDocument = await self.repository.undelete(
                record_id, source=SOURCE_UNDO
            )
            return revived.revision_no
        now = found.model_dump(mode="json")
        ours = [name for name, wrote in step.fields.items() if now.get(name) == wrote]
        if not ours:
            return None
        changed: RevisionedDocument = await self.repository.update(
            record_id,
            {name: back.fields[name] for name in ours},
            expected=found.revision_no,
            source=SOURCE_UNDO,
        )
        return changed.revision_no

    async def follow(self, record_id: str) -> None:
        """Nothing, for most kinds of record."""


def _back(
    step: Step,
    op: StepOp,
    record_id: str,
    expected: int,
    fields: dict[str, Any] | None = None,
) -> Step:
    return Step(
        entity=step.entity, op=op, record_id=record_id, fields=fields or {}, expected=expected
    )


class TripStore(RevisionedStore):
    """A trip's dates always hold all its activities and everything on its checklist, and a
    traveller stays while items or expenses name them. Its reminder follows each write, an
    undo's too, once the change is recorded. A person keeps so many trips, not a trip so many."""

    def __init__(self, ctx: ActorContext) -> None:
        super().__init__(TripRepository(ctx.scope, ctx.clock))
        self.ctx = ctx
        self.activities = ActivityRepository(ctx.scope, ctx.clock)
        self.checklist = ChecklistRepository(ctx.scope, ctx.clock)
        self.expenses = ExpenseRepository(ctx.scope, ctx.clock)

    async def ensure_room(self, record: Mapping[str, Any]) -> None:  # noqa: ARG002 - the person's
        if await Trip.find(self.repository.query()).count() >= rules().trips.max_per_user:
            raise TripsFullError

    async def check(self, record: Mapping[str, Any]) -> None:
        trip_id, start, end = record["id"], record["start_date"], record["end_date"]
        days = await self.activities.days_outside(trip_id, start, end)
        if days:
            raise ActivitiesOutsideDatesError(details={"days": days})
        days = await self.checklist.days_outside(trip_id, start, end)
        if days:
            raise ChecklistOutsideDatesError(details={"days": days})
        staying = {traveller["id"] for traveller in record["travellers"]}
        named = {
            *await self.checklist.travellers_named(trip_id),
            *await self.expenses.travellers_named(trip_id),
        }
        gone = sorted(named - staying)
        if gone:
            raise TravellerInUseError(details={"traveller_ids": gone})

    async def follow(self, record_id: str) -> None:
        trip = await self.repository.get(record_id, include_deleted=True)
        await sync_reminder(self.ctx, trip)


class ActivityStore(RevisionedStore):
    """An activity's day is always within its trip's dates. Its making can't be taken back once
    checklist items or expenses are tied to it: they would be left on an activity that never
    was. Nor can an undo link it again to a booking removed since."""

    def __init__(self, ctx: ActorContext) -> None:
        room = Room(lambda: rules().activities.max_per_trip, ActivitiesFullError)
        super().__init__(ActivityRepository(ctx.scope, ctx.clock), room)
        self.trips = TripRepository(ctx.scope, ctx.clock)
        self.checklist = ChecklistRepository(ctx.scope, ctx.clock)
        self.expenses = ExpenseRepository(ctx.scope, ctx.clock)
        self.bookings = BookingRepository(ctx.scope, ctx.clock)

    async def check(self, record: Mapping[str, Any]) -> None:
        ensure_in_trip(await self.trips.get(record["trip_id"]), record["day"])

    async def stale(self, step: Step) -> bool:
        tied = {"activity_id": step.record_id}
        if step.op is StepOp.REMOVE and (
            await self.checklist.first(tied) or await self.expenses.first(tied)
        ):
            return True
        if step.op is StepOp.REVIVE or "booking_id" in step.fields:
            activity = await self.repository.find(step.record_id or "", include_deleted=True)
            if activity is not None and await self._booking_gone(
                activity.model_copy(update=step.fields)
            ):
                return True
        return await super().stale(step)

    async def _booking_gone(self, activity: TripActivity) -> bool:
        if activity.booking_id is None:
            return False
        booking = await self.bookings.find(activity.booking_id)
        return booking is None or booking.trip_id != activity.trip_id


class ChecklistStore(RevisionedStore):
    """An item's day, if it has one, is within its trip's dates, and a suggestion is there once.
    An undo is stale once it would leave an item naming someone no longer on the trip, or off the
    day of the activity it is tied to: what the item names has changed since."""

    def __init__(self, ctx: ActorContext) -> None:
        room = Room(lambda: rules().checklist.max_per_trip, ChecklistFullError)
        super().__init__(ChecklistRepository(ctx.scope, ctx.clock), room)
        self.trips = TripRepository(ctx.scope, ctx.clock)
        self.activities = ActivityRepository(ctx.scope, ctx.clock)

    async def check(self, record: Mapping[str, Any]) -> None:
        trip = await self.trips.get(record["trip_id"])
        if record.get("day"):
            ensure_in_trip(trip, record["day"])

    async def stale(self, step: Step) -> bool:
        # A change to an activity writes only its items' day or state, and takes them back so:
        # the activity itself moves back in the same undo.
        if step.op is StepOp.REVIVE or {"activity_id", "traveller_id"} & step.fields.keys():
            item = await self.repository.find(step.record_id or "", include_deleted=True)
            if item is not None and await self._out_of_place(item.model_copy(update=step.fields)):
                return True
        return await super().stale(step)

    async def _out_of_place(self, item: ChecklistItem) -> bool:
        trip = await self.trips.get(item.trip_id)
        if item.traveller_id and item.traveller_id not in {each.id for each in trip.travellers}:
            return True
        activity = await self.activities.find(item.activity_id) if item.activity_id else None
        return activity is not None and activity.day != item.day

    async def write(self, step: Step, back: Step, source: str) -> Step:
        try:
            return await super().write(step, back, source)
        except DuplicateKeyError as exc:
            raise SuggestionTakenError from exc


class BookingStore(RevisionedStore):
    """A booking's making can't be taken back while an activity links to it."""

    def __init__(self, ctx: ActorContext) -> None:
        room = Room(lambda: rules().bookings.max_per_trip, BookingsFullError)
        super().__init__(BookingRepository(ctx.scope, ctx.clock), room)
        self.activities = ActivityRepository(ctx.scope, ctx.clock)

    async def stale(self, step: Step) -> bool:
        if step.op is StepOp.REMOVE and await self.activities.first({"booking_id": step.record_id}):
            return True
        return await super().stale(step)


class ExpenseStore(RevisionedStore):
    """An undo is stale once it would leave an expense naming someone no longer on the trip."""

    def __init__(self, ctx: ActorContext) -> None:
        room = Room(lambda: rules().budget.max_expenses_per_trip, ExpensesFullError)
        super().__init__(ExpenseRepository(ctx.scope, ctx.clock), room)
        self.trips = TripRepository(ctx.scope, ctx.clock)

    async def stale(self, step: Step) -> bool:
        if step.op is StepOp.REVIVE or {"paid_by", "shared_with"} & step.fields.keys():
            expense = await self.repository.find(step.record_id or "", include_deleted=True)
            if expense is not None and await self._someone_gone(
                expense.model_copy(update=step.fields)
            ):
                return True
        return await super().stale(step)

    async def _someone_gone(self, expense: TripExpense) -> bool:
        trip = await self.trips.get(expense.trip_id)
        named = {expense.paid_by, *expense.shared_with} - {None}
        return not named <= {traveller.id for traveller in trip.travellers}


# The kinds of record a change can reach, by a step's ``entity``.
STORES: dict[str, Callable[[ActorContext], Store]] = {
    TRIP: TripStore,
    ACTIVITY: ActivityStore,
    CHECKLIST: ChecklistStore,
    BOOKING: BookingStore,
    EXPENSE: ExpenseStore,
}


# ---------------------------------------------------------------------- the history


@dataclass
class Ran:
    """How a run of steps went: what takes back each step written (or being written) and, once
    one failed, the failure, the places of those still written, and those put back just as they
    were (with the revision each is at now)."""

    backs: list[Step] = field(default_factory=list)
    failed: Exception | None = None
    stayed: list[int] = field(default_factory=list)
    put_back: dict[int, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Saved(Generic[T]):
    """What a write left, and the event of the trip's history it recorded: none when it changed
    nothing, so the page undoes exactly the one it made."""

    record: T
    event: TripEvent | None = None

    @property
    def event_id(self) -> str | None:
        return None if self.event is None else str(self.event.id)


class Events:
    def __init__(self, ctx: ActorContext) -> None:
        self.ctx = ctx
        self.repository = EventRepository(ctx.scope, ctx.clock)
        self.trips = TripRepository(ctx.scope, ctx.clock)

    def _store(self, step: Step) -> Store:
        return STORES[step.entity](self.ctx)

    async def record(
        self,
        trip_id: PydanticObjectId | None,
        kind: str,
        *,
        summary: Mapping[str, Any] | None = None,
        changes: Sequence[RecordChange] = (),
        undo: Sequence[Step] = (),
        ai_run_id: str | None = None,
    ) -> TripEvent:
        return await self.repository.create(
            {
                "trip_id": trip_id,
                "at": self.ctx.clock.now(),
                "kind": kind,
                "summary": dict(summary or {}),
                "changes": list(changes),
                "undo": list(undo),
                "source": EventSource.AI if ai_run_id else EventSource.MANUAL,
                "ai_run_id": ai_run_id,
            }
        )

    async def record_made(
        self,
        trip_id: PydanticObjectId | None,
        kind: str,
        entity: str,
        record: RevisionedDocument,
        *,
        title: str,
        day: str | None = None,
    ) -> TripEvent:
        """A record already made (so once per Idempotency-Key), taken back by removing it."""
        record_id = str(record.id)
        return await self.record(
            trip_id,
            kind,
            summary={"title": title, "day": day},
            changes=[
                RecordChange(
                    entity=entity, op=StepOp.ADD, record_id=record_id, title=title, day=day
                )
            ],
            undo=[
                Step(
                    entity=entity,
                    op=StepOp.REMOVE,
                    record_id=record_id,
                    expected=record.revision_no,
                )
            ],
        )

    async def made(
        self, record: Made, kind: str, trip_id: PydanticObjectId | None = None
    ) -> Saved[Made]:
        """A record an earlier request with the same Idempotency-Key made, with the event that
        recorded its making (``kind``), as the first answer had it. ``trip_id``: the trip it is
        of; none, it is the trip."""
        found: dict[str, Any] = {"trip_id": trip_id or record.id, "kind": kind}
        if trip_id is not None:
            found["changes.record_id"] = str(record.id)
        return Saved(record, await self.repository.first(found))

    async def commit(
        self,
        trip_id: PydanticObjectId | None,
        kind: str,
        planned: Sequence[Planned],
        *,
        summary: Mapping[str, Any] | None = None,
        ai_run_id: str | None = None,
    ) -> TripEvent | None:
        """Write what is planned, all of it or none of it, and record it. None when it changes
        nothing: no event then. What follows the writes runs once it is recorded. ``ai_run_id``:
        the person applied that AI run's draft, so the event and the revisions say AI made it."""
        writes = writing(planned)
        if not writes:
            return None

        source = EventSource.AI if ai_run_id else EventSource.MANUAL
        ran = await self._run([item.step for item in writes], source.value)

        async def recorded(places: Sequence[int]) -> TripEvent:
            backs = [ran.backs[place] for place in places]
            changes = [
                writes[place].shown.model_copy(update={"record_id": ran.backs[place].record_id})
                for place in places
            ]
            return await self.record(
                trip_id,
                kind,
                summary=summary,
                changes=changes,
                undo=list(reversed(backs)),
                ai_run_id=ai_run_id,
            )

        if ran.failed:
            if ran.stayed:  # still written: the history shows it all the same, to take it back
                await recorded(ran.stayed)
                await self._follow([ran.backs[place] for place in ran.stayed], quietly=True)
            raise ran.failed
        event = await recorded(range(len(writes)))
        await self._follow(ran.backs)
        return event

    async def _run(self, steps: Sequence[Step], source: str) -> Ran:
        """Each step in turn. Once one fails, whyever, what was written is taken back."""
        ran = Ran()
        try:
            for step in steps:
                await self._step(step, source, ran)
        except Exception as error:  # all of them: a change half written is never left unsaid
            ran.failed = error
            await self._take_back(steps, ran)
        return ran

    async def _step(self, step: Step, source: str, ran: Ran) -> None:
        """What takes the step back is kept before it is written: a write that lands and then
        fails is taken back too. One refused (an ``AppError``: moved on since, say) wrote
        nothing, and what someone else wrote meanwhile stays theirs."""
        store = self._store(step)
        ran.backs.append(await store.ready(step))
        try:
            ran.backs[-1] = await store.write(step, ran.backs[-1], source)
        except AppError:
            ran.backs.pop()
            raise

    async def _follow(self, backs: Sequence[Step], *, quietly: bool = False) -> None:
        """What follows the writes to these records, once recorded: each record once. After a
        change that failed (``quietly``), its own failure is what answers."""
        for back in {(back.entity, back.record_id): back for back in backs}.values():
            try:
                await self._store(back).follow(back.record_id or "")
            except Exception:
                if not quietly:
                    raise
                logger.exception("travel_follow_up_failed", entity=back.entity)

    async def _take_back(self, steps: Sequence[Step], ran: Ran) -> None:
        """The steps written, taken back the latest first, each on its own: one that can't be
        doesn't stop the rest."""
        for place in reversed(range(len(ran.backs))):
            back = ran.backs[place]
            try:
                revision = await self._store(back).revert(steps[place], back)
            except Exception:
                logger.exception("travel_take_back_failed", entity=back.entity, op=back.op)
                ran.stayed.insert(0, place)
            else:
                if revision is not None:
                    ran.put_back[place] = revision

    async def page(self, trip_id: str, params: PageParams) -> tuple[list[TripEvent], int]:
        trip = await self.trips.get(trip_id)
        return await self.repository.list(params, NEWEST_EVENT, {"trip_id": trip.id})

    async def undo(self, event_id: str) -> TripEvent:
        """Take the event back; what answers is the undo's own event. One that fails, whyever,
        is put back as the event left it and can be tried again. What of it couldn't be put back
        is recorded as undone that far, and the event is then stale: what it touched moved on."""
        event = await self.repository.get(event_id)
        await self.trips.get(event.trip_id)  # a deleted trip's history is out of reach
        if not event.undo:
            raise UndoUnavailableError
        if event.undone_at:
            raise UndoDoneError
        stale = [step.record_id for step in event.undo if await self._store(step).stale(step)]
        if stale:
            raise UndoStaleError(details={"record_ids": stale})
        if not await self._claim(event):
            raise UndoDoneError  # another request got there first
        ran = await self._run(event.undo, SOURCE_UNDO)
        if ran.failed:
            touched = {event.undo[place].record_id for place in ran.stayed}
            if touched:
                await self._undid(event, [it for it in event.changes if it.record_id in touched])
                await self._follow([ran.backs[place] for place in ran.stayed], quietly=True)
            await self._reopen(event, ran.put_back)
            if isinstance(ran.failed, VersionConflictError):
                raise UndoStaleError from ran.failed
            raise ran.failed
        undone = await self._undid(event, event.changes)
        await self._follow(ran.backs)
        return undone

    async def _undid(self, event: TripEvent, changes: Sequence[RecordChange]) -> TripEvent:
        """The undo's own event: these changes of ``event``, taken back."""
        return await self.record(
            event.trip_id,
            UNDO_KIND,
            summary={**event.summary, "undid": event.kind, "event_id": str(event.id)},
            changes=[_taken_back(change) for change in reversed(changes)],
        )

    async def _claim(self, event: TripEvent) -> bool:
        """Mark it undone, only if it isn't: whether this call did it."""
        now = self.ctx.clock.now()
        result = await TripEvent.get_pymongo_collection().update_one(
            self.repository.query({"_id": event.id, "undone_at": None}),
            {"$set": {"undone_at": now, "updated_at": now}},
        )
        return bool(result.matched_count)

    async def _reopen(self, event: TripEvent, put_back: Mapping[int, int]) -> None:
        """An undo that failed: not undone after all. A record it put back just as the event
        left it is now at a later revision, which its step expects from now on."""
        moved = {f"undo.{place}.expected": revision for place, revision in put_back.items()}
        await TripEvent.get_pymongo_collection().update_one(
            self.repository.query({"_id": event.id}),
            {"$set": {"undone_at": None, "updated_at": self.ctx.clock.now(), **moved}},
        )


def _taken_back(change: RecordChange) -> RecordChange:
    lines = [FieldChange(field=item.field, old=item.new, new=item.old) for item in change.fields]
    return change.model_copy(update={"op": OPPOSITE[change.op], "fields": lines})
