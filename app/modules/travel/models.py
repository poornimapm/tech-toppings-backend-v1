"""What Travel keeps, all owned by one person and, below the trip, by one trip: the trip with its
travellers, its activities, its checklist and packing items, its bookings and expenses, and the
trip's history of changes. Days are YYYY-MM-DD and local times HH:MM, at the destination; money is
in minor units, of the trip's currency unless the record names its own.

Every record but an event keeps every revision and is soft-deleted. Only their own fields are ever
written: a change to one record never rewrites another's."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime
from enum import StrEnum
from typing import Any, ClassVar, TypeVar
from zoneinfo import ZoneInfo

from beanie import PydanticObjectId, SortDirection
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.modules.travel.rules import PackingBag, rules
from app.platform import (
    CLIENT_KEY_INDEX,
    REVISION_INDEXES,
    ActorContext,
    FieldChange,
    OwnedDocument,
    PageParams,
    Revision,
    RevisionAction,
    RevisionedDocument,
    RevisionedRepository,
    ScopedRepository,
    parse_object_id,
)

MODULE = "travel"
SOURCE_UNDO = "undo"  # how a revision made by taking a change back says so
# A day's order; a replacement shares the place of what it replaced, and comes right after it.
TIMELINE: tuple[tuple[str, int], ...] = (("day", 1), ("position", 1), ("_id", 1))
# Those without a day first, then by day; on a day, as they were added.
CHECKLIST_ORDER: tuple[tuple[str, int], ...] = (("day", 1), ("_id", 1))
NEWEST_EVENT: tuple[tuple[str, int], ...] = (("at", -1), ("_id", -1))
# Those with no time (a visa, a policy) first, then as they start.
BOOKING_ORDER: tuple[tuple[str, int], ...] = (("starts_at", 1), ("_id", 1))
EXPENSE_ORDER: tuple[tuple[str, int], ...] = (("day", 1), ("_id", 1))


class TripType(StrEnum):
    HONEYMOON = "honeymoon"
    SOLO = "solo"
    FRIENDS = "friends"
    FAMILY = "family"
    BUSINESS = "business"
    ADVENTURE = "adventure"
    OTHER = "other"


class Pace(StrEnum):
    RELAXED = "relaxed"
    BALANCED = "balanced"
    PACKED = "packed"


class TripMode(StrEnum):
    STATIC = "static"
    BASIC = "basic"
    PREMIUM = "premium"


class TripPhase(StrEnum):
    UPCOMING = "upcoming"
    ACTIVE = "active"
    PAST = "past"


class Destination(BaseModel):
    name: str
    country: str
    timezone: str | None = None


class Traveller(BaseModel):
    """One person on the trip. Each has their own passport country, and nothing is inferred."""

    id: str
    name: str
    passport_country: str | None = None
    residence_country: str | None = None
    passport_expiry: str | None = None
    dietary: str | None = None
    accessibility: str | None = None


class Trip(RevisionedDocument):
    MODULE_KEY: ClassVar[str] = MODULE

    title: str
    destinations: list[Destination]
    departure_city: str | None = None
    start_date: str
    end_date: str
    trip_type: TripType = TripType.OTHER
    pace: Pace = Pace.BALANCED
    budget_minor: int | None = None
    currency: str
    mode: TripMode = TripMode.STATIC
    interests: list[str] = Field(default_factory=list)
    notes: str | None = None
    remind_prepare: bool = False
    travellers: list[Traveller] = Field(default_factory=list)

    class Settings:
        name = "travel_trips"
        indexes = [  # noqa: RUF012 - Beanie reads this class attribute
            CLIENT_KEY_INDEX,
            IndexModel([("user_id", ASCENDING), ("start_date", DESCENDING)], name="user_by_start"),
        ]


class TripRevision(Revision):
    class Settings:
        name = "travel_trip_revisions"
        indexes = REVISION_INDEXES


def trip_today(trip: Trip, ctx: ActorContext) -> date:
    zone = trip.destinations[0].timezone
    return ctx.clock.now().astimezone(ZoneInfo(zone)).date() if zone else ctx.today()


class ActivityStatus(StrEnum):
    PLANNED = "planned"
    DONE = "done"
    SKIPPED = "skipped"
    REPLACED = "replaced"


class TripActivity(RevisionedDocument):
    """One thing on one day. ``confirmed`` false means its details are an estimate;
    ``actual_note`` is what really happened, kept beside the plan."""

    MODULE_KEY: ClassVar[str] = MODULE

    trip_id: PydanticObjectId
    day: str
    start_time: str | None = None
    duration_minutes: int | None = None
    title: str
    kind: str
    location: str | None = None
    transport: str | None = None
    notes: str | None = None
    estimated_cost_minor: int | None = None
    confirmed: bool = False
    booking_id: str | None = None
    position: int = 0
    status: ActivityStatus = ActivityStatus.PLANNED
    actual_note: str | None = None

    class Settings:
        name = "travel_activities"
        indexes = [  # noqa: RUF012
            CLIENT_KEY_INDEX,
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("trip_id", ASCENDING),
                    ("day", ASCENDING),
                    ("position", ASCENDING),
                ],
                name="trip_timeline",
            ),
        ]


class ActivityRevision(Revision):
    class Settings:
        name = "travel_activity_revisions"
        indexes = REVISION_INDEXES


class ChecklistState(StrEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    NOT_APPLICABLE = "not_applicable"


OPEN_STATES = (ChecklistState.NOT_STARTED, ChecklistState.IN_PROGRESS)  # still to do


class ChecklistSource(StrEnum):
    RULE = "rule"
    USER = "user"
    ASSISTANT = "assistant"


class ChecklistItem(RevisionedDocument):
    """One thing to do or to bring. With a ``bag`` it is a packing item: there is no second store.
    One tied to an activity is on that activity's day.

    ``edited`` turns true once the person changes it, for good: from then on it is their own work.
    A suggestion carries its ``suggestion_key``, and a row with that key stays once the person
    removes it (``dismissed``), which is what keeps it from being suggested again."""

    MODULE_KEY: ClassVar[str] = MODULE

    trip_id: PydanticObjectId
    day: str | None = None
    activity_id: str | None = None
    traveller_id: str | None = None
    title: str
    state: ChecklistState = ChecklistState.NOT_STARTED
    source: ChecklistSource = ChecklistSource.USER
    edited: bool = False
    bag: PackingBag | None = None
    quantity: int = 1
    suggestion_key: str | None = None
    dismissed: bool = False

    @property
    def as_suggested(self) -> bool:
        """Still the suggestion as its rule words it, so the app shows its own words for it, as a
        suggestion. Renamed, it is in the person's words; a tick or a new quantity isn't that."""
        worded = rules().suggestions.words_of(self.suggestion_key)
        return self.source is ChecklistSource.RULE and self.title in worded

    @property
    def worded_by(self) -> str | None:
        """The suggestion the app words it by, while it is as suggested."""
        return self.suggestion_key if self.as_suggested else None

    class Settings:
        name = "travel_checklist"
        indexes = [  # noqa: RUF012
            CLIENT_KEY_INDEX,
            IndexModel(
                [("user_id", ASCENDING), ("trip_id", ASCENDING), ("day", ASCENDING)],
                name="trip_days",
            ),
            # One live row per suggestion, whoever asks at the same moment.
            IndexModel(
                [("user_id", ASCENDING), ("trip_id", ASCENDING), ("suggestion_key", ASCENDING)],
                unique=True,
                partialFilterExpression={"suggestion_key": {"$type": "string"}, "deleted_at": None},
                name="suggestion_once",
            ),
        ]


class ChecklistRevision(Revision):
    class Settings:
        name = "travel_checklist_revisions"
        indexes = REVISION_INDEXES


class BookingKind(StrEnum):
    FLIGHT = "flight"
    HOTEL = "hotel"
    TOUR = "tour"
    TRANSFER = "transfer"
    INSURANCE = "insurance"
    VISA = "visa"
    OTHER = "other"


class BookingStatus(StrEnum):
    ENTERED = "entered"  # typed in by the person
    EXTRACTED = "extracted"  # read from a document the person gave (Phase 21: nothing does yet)
    CONFIRMED = "confirmed"  # the person says they checked it with whoever it is with
    VERIFIED = "verified"  # checked with the provider by the app (nothing does yet)


class Booking(RevisionedDocument):
    """Something the person booked, as they entered it: the app never makes one up. Its times
    are local where it happens, ``YYYY-MM-DDTHH:MM``, and may fall outside the trip's dates (a
    visa is booked well before)."""

    MODULE_KEY: ClassVar[str] = MODULE

    trip_id: PydanticObjectId
    kind: BookingKind
    title: str
    reference: str | None = None
    starts_at: str | None = None
    ends_at: str | None = None
    location: str | None = None
    notes: str | None = None
    link: str | None = None
    status: BookingStatus = BookingStatus.ENTERED

    class Settings:
        name = "travel_bookings"
        indexes = [  # noqa: RUF012
            CLIENT_KEY_INDEX,
            IndexModel(
                [("user_id", ASCENDING), ("trip_id", ASCENDING), ("starts_at", ASCENDING)],
                name="trip_bookings",
            ),
        ]


class BookingRevision(Revision):
    class Settings:
        name = "travel_booking_revisions"
        indexes = REVISION_INDEXES


class ExpenseKind(StrEnum):
    ESTIMATE = "estimate"
    ACTUAL = "actual"


class TripExpense(RevisionedDocument):
    """A cost of the trip, estimated or really spent, in its own currency: the budget converts
    it on its ``day`` (when it was paid or is for, inside the trip's dates or not). Who paid it
    and who share it are travellers of the trip; shared by nobody, it is the payer's own."""

    MODULE_KEY: ClassVar[str] = MODULE

    trip_id: PydanticObjectId
    activity_id: str | None = None
    kind: ExpenseKind
    category: str
    title: str
    amount_minor: int
    currency: str
    day: str | None = None
    paid_by: str | None = None
    shared_with: list[str] = Field(default_factory=list)

    class Settings:
        name = "travel_expenses"
        indexes = [  # noqa: RUF012
            CLIENT_KEY_INDEX,
            IndexModel(
                [("user_id", ASCENDING), ("trip_id", ASCENDING), ("day", ASCENDING)],
                name="trip_expenses",
            ),
        ]


class ExpenseRevision(Revision):
    class Settings:
        name = "travel_expense_revisions"
        indexes = REVISION_INDEXES


class StepOp(StrEnum):
    SET = "set"  # change these fields
    ADD = "add"  # make a record of these fields
    REMOVE = "remove"  # soft-delete it
    REVIVE = "revive"  # bring it back


class Step(BaseModel):
    """One write to one record, as data. A change is a list of them, and what undoes it is the
    list that takes them back (events.py). Only JSON values in ``fields``, as revisions need."""

    entity: str  # which store writes it (events.STORES)
    op: StepOp
    record_id: str | None = None  # none yet, for ADD
    fields: dict[str, Any] = Field(default_factory=dict)
    expected: int | None = None  # the version it must still be at (not for ADD)


class RecordChange(BaseModel):
    """One record a change reached, as the page shows it under "What changed?"."""

    entity: str
    op: StepOp
    record_id: str | None = None  # none in a preview of one not made yet
    title: str
    day: str | None = None
    fields: list[FieldChange] = Field(default_factory=list)
    # A suggestion still as its rule words it: the page shows its own words for it, by this key.
    suggestion_key: str | None = None


class TripEvent(OwnedDocument):
    """One change to a trip: what kind, the words for its summary, what it reached and how to
    take it back. Written only by events.py; no route edits or removes one."""

    MODULE_KEY: ClassVar[str] = MODULE

    trip_id: PydanticObjectId
    at: datetime
    kind: str  # trip_created, activity_skipped, undo ...: worded on the app, by key
    summary: dict[str, Any] = Field(default_factory=dict)
    changes: list[RecordChange] = Field(default_factory=list)
    undo: list[Step] = Field(default_factory=list)  # empty: this one can't be undone
    undone_at: datetime | None = None

    class Settings:
        name = "travel_events"
        indexes = [  # noqa: RUF012
            IndexModel(
                [("user_id", ASCENDING), ("trip_id", ASCENDING), ("at", DESCENDING)],
                name="trip_history",
            ),
        ]


class TripRepository(RevisionedRepository[Trip]):
    model: ClassVar[type[OwnedDocument]] = Trip
    revisions: ClassVar[type[Revision]] = TripRevision


class ActivityRepository(RevisionedRepository[TripActivity]):
    model: ClassVar[type[OwnedDocument]] = TripActivity
    revisions: ClassVar[type[Revision]] = ActivityRevision

    async def of_trip(
        self, trip_id: PydanticObjectId | None, day: str | None = None
    ) -> list[TripActivity]:
        """The trip's activities, or one day's, in timeline order."""
        filters: dict[str, Any] = {"trip_id": trip_id, **({"day": day} if day else {})}
        whole = PageParams(1, rules().activities.max_per_trip)
        return (await self.list(whole, TIMELINE, filters))[0]

    async def count_of(self, trip_id: PydanticObjectId | None) -> int:
        return await TripActivity.find(self.query({"trip_id": trip_id})).count()

    async def next_position(self, trip_id: PydanticObjectId | None, day: str) -> int:
        """After the day's last activity. Gaps are fine: only the order counts."""
        last = await (
            TripActivity.find(self.query({"trip_id": trip_id, "day": day}))
            .sort([("position", SortDirection.DESCENDING)])
            .first_or_none()
        )
        return last.position + 1 if last else 0

    async def days_outside(
        self, trip_id: PydanticObjectId | None, start: str, end: str
    ) -> list[str]:
        """The days holding an activity that the dates ``start`` to ``end`` leave out."""
        outside = {"trip_id": trip_id, "$or": [{"day": {"$lt": start}}, {"day": {"$gt": end}}]}
        days = await TripActivity.get_pymongo_collection().distinct("day", self.query(outside))
        return sorted(days)

    async def ahead(
        self, trip_id: PydanticObjectId | None, day: str, limit: int
    ) -> list[TripActivity]:
        """The first ``limit`` still planned from ``day`` on, in timeline order."""
        filters = {"trip_id": trip_id, "day": {"$gte": day}, "status": ActivityStatus.PLANNED}
        return (await self.list(PageParams(1, limit), TIMELINE, filters))[0]

    async def linking(self, booking_id: str | None) -> list[str]:
        """The activities that link to this booking, by id."""
        found = await TripActivity.find(self.query({"booking_id": booking_id})).to_list()
        return [str(activity.id) for activity in found]


class ChecklistRepository(RevisionedRepository[ChecklistItem]):
    model: ClassVar[type[OwnedDocument]] = ChecklistItem
    revisions: ClassVar[type[Revision]] = ChecklistRevision

    async def of_trip(
        self, trip_id: PydanticObjectId | None, **filters: Any
    ) -> list[ChecklistItem]:
        whole = PageParams(1, rules().checklist.max_per_trip)
        return (await self.list(whole, CHECKLIST_ORDER, {"trip_id": trip_id, **filters}))[0]

    async def tied_to(self, activity: TripActivity) -> list[ChecklistItem]:
        return await self.of_trip(activity.trip_id, activity_id=str(activity.id))

    async def count_of(self, trip_id: PydanticObjectId | None) -> int:
        return await ChecklistItem.find(self.query({"trip_id": trip_id})).count()

    async def _distinct(self, field: str, criteria: Mapping[str, Any], **query: Any) -> list[str]:
        found = await ChecklistItem.get_pymongo_collection().distinct(
            field, self.query(criteria, **query)
        )
        return sorted(value for value in found if value)

    async def days_outside(
        self, trip_id: PydanticObjectId | None, start: str, end: str
    ) -> list[str]:
        """The days holding an item that the dates ``start`` to ``end`` leave out. One that no
        longer applies holds no day: it needs nothing done on it."""
        return await self._distinct(
            "day",
            {
                "trip_id": trip_id,
                "state": {"$ne": ChecklistState.NOT_APPLICABLE},
                "$or": [{"day": {"$lt": start}}, {"day": {"$gt": end}}],
            },
        )

    async def travellers_named(self, trip_id: PydanticObjectId | None) -> list[str]:
        return await self._distinct("traveller_id", {"trip_id": trip_id})

    async def tally(
        self, trip_id: PydanticObjectId | None
    ) -> dict[tuple[ChecklistState, bool], int]:
        """How many items are in each state, packing items (those with a bag) apart."""
        packing = {"$ne": [{"$ifNull": ["$bag", None]}, None]}
        cursor = await ChecklistItem.get_pymongo_collection().aggregate(
            [
                {"$match": self.query({"trip_id": trip_id})},
                {"$group": {"_id": {"state": "$state", "packing": packing}, "n": {"$sum": 1}}},
            ]
        )
        return {
            (ChecklistState(row["_id"]["state"]), bool(row["_id"]["packing"])): int(row["n"])
            async for row in cursor
        }

    async def open_on(self, trip_id: PydanticObjectId | None, day: str) -> int:
        """The day's items still to do."""
        criteria = {"trip_id": trip_id, "day": day, "state": {"$in": list(OPEN_STATES)}}
        return await ChecklistItem.find(self.query(criteria)).count()

    async def open_days(
        self, trip_id: PydanticObjectId | None, *, packing: bool | None = None
    ) -> list[str]:
        """The days holding items still to do, in order: of packing items, of the others, or
        (``None``) of all of them. The whole trip's, with no day, aren't a day's."""
        criteria: dict[str, Any] = {"trip_id": trip_id, "state": {"$in": list(OPEN_STATES)}}
        if packing is not None:
            criteria["bag"] = {"$ne": None} if packing else None
        return await self._distinct("day", criteria)

    async def taken_keys(self, trip_id: PydanticObjectId | None) -> list[str]:
        """The suggestions not to make again: there now, or removed by the person. One an undo
        took back was never decided on, so it can be offered again."""
        return await self._distinct(
            "suggestion_key",
            {"trip_id": trip_id, "$or": [{"deleted_at": None}, {"dismissed": True}]},
            include_deleted=True,
        )

    async def soft_delete(self, entity_id: str | PydanticObjectId, *, source: str = "api") -> None:
        """Removed by the person, it counts as dismissed; taken back by an undo, it doesn't."""
        dismissed = {"dismissed": source != SOURCE_UNDO}
        await self._revise(entity_id, dismissed, RevisionAction.DELETE, None, source)

    async def undelete(
        self, entity_id: str | PydanticObjectId, *, source: str = "api"
    ) -> ChecklistItem:
        return await self._revise(
            entity_id, {"dismissed": False}, RevisionAction.UNDELETE, None, source
        )


class BookingRepository(RevisionedRepository[Booking]):
    model: ClassVar[type[OwnedDocument]] = Booking
    revisions: ClassVar[type[Revision]] = BookingRevision

    async def of_trip(
        self, trip_id: PydanticObjectId | None, filters: Mapping[str, Any] | None = None
    ) -> list[Booking]:
        whole = PageParams(1, rules().bookings.max_per_trip)
        return (await self.list(whole, BOOKING_ORDER, {"trip_id": trip_id, **(filters or {})}))[0]

    async def count_of(
        self, trip_id: PydanticObjectId | None, filters: Mapping[str, Any] | None = None
    ) -> int:
        return await Booking.find(self.query({"trip_id": trip_id, **(filters or {})})).count()

    async def on_day(
        self, trip_id: PydanticObjectId | None, day: str, linked: Iterable[str | None]
    ) -> list[Booking]:
        """Those starting or ending on the day, and those the day's activities link to."""
        within = {"$gte": f"{day}T00:00", "$lte": f"{day}T23:59"}
        ids = [found for found in map(parse_object_id, filter(None, linked)) if found]
        return await self.of_trip(
            trip_id, {"$or": [{"starts_at": within}, {"ends_at": within}, {"_id": {"$in": ids}}]}
        )


class ExpenseRepository(RevisionedRepository[TripExpense]):
    model: ClassVar[type[OwnedDocument]] = TripExpense
    revisions: ClassVar[type[Revision]] = ExpenseRevision

    async def of_trip(self, trip_id: PydanticObjectId | None, **filters: Any) -> list[TripExpense]:
        whole = PageParams(1, rules().budget.max_expenses_per_trip)
        return (await self.list(whole, EXPENSE_ORDER, {"trip_id": trip_id, **filters}))[0]

    async def count_of(self, trip_id: PydanticObjectId | None) -> int:
        return await TripExpense.find(self.query({"trip_id": trip_id})).count()

    async def travellers_named(self, trip_id: PydanticObjectId | None) -> list[str]:
        """Everyone an expense names, as its payer or as one sharing it."""
        collection = TripExpense.get_pymongo_collection()
        criteria = self.query({"trip_id": trip_id})
        named = [
            *await collection.distinct("paid_by", criteria),
            *await collection.distinct("shared_with", criteria),  # each of a list, one by one
        ]
        return sorted({value for value in named if value})


class EventRepository(ScopedRepository[TripEvent]):
    model: ClassVar[type[OwnedDocument]] = TripEvent


Kept = TypeVar("Kept", bound=RevisionedDocument)


async def made_with(repository: RevisionedRepository[Kept], key: str | None) -> Kept | None:
    """What an earlier request with this Idempotency-Key made, removed since or not. A retry
    answers it before any check the first request passed: one that took the last place would
    otherwise be told there is no room."""
    if key is None:
        return None
    found = await repository.model.find_one(
        repository.query({"client_key": key}, include_deleted=True)
    )
    return None if found is None else await repository.find(str(found.id), include_deleted=True)
