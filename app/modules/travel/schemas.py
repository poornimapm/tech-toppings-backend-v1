"""What the routes take and give. The answers are named after the module (``Travel...``), so its
API types stay its own."""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated, Any, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)

from app.modules.travel.models import (
    ActivityStatus,
    Booking,
    BookingKind,
    BookingStatus,
    ChecklistItem,
    ChecklistSource,
    ChecklistState,
    Destination,
    ExpenseKind,
    Pace,
    RecordChange,
    Traveller,
    TripActivity,
    TripEvent,
    TripExpense,
    TripMode,
    TripPhase,
    TripType,
)
from app.modules.travel.rules import NextAction, PackingBag, rules

MAX_MINOR = 10**13  # the largest amount taken, as everywhere in the app


def _blank_is_none(value: object) -> object:
    return None if isinstance(value, str) and not value.strip() else value


def _known_zone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:  # OSError: a folder of zones
        raise ValueError("not a time zone") from exc
    return value


def _known_kind(value: str) -> str:
    if value not in rules().activities.kinds:
        raise ValueError("not a kind of activity")
    return value


def _known_category(value: str) -> str:
    if value not in rules().budget.categories:
        raise ValueError("not a budget category")
    return value


def _real_moment(value: str) -> str:
    dt.datetime.fromisoformat(value)  # a day that doesn't exist (30 February) is refused
    return value


def _each_once(value: list[str]) -> list[str]:
    if len(set(value)) != len(value):
        raise ValueError("each traveller once")
    return value


Blank = BeforeValidator(_blank_is_none)  # an emptied field is no value
# Optional text up to a length (the length is the text's: none is no text).
Text80 = Annotated[Annotated[str, Field(max_length=80)] | None, Blank]
Text200 = Annotated[Annotated[str, Field(max_length=200)] | None, Blank]
Text500 = Annotated[Annotated[str, Field(max_length=500)] | None, Blank]
Text1000 = Annotated[Annotated[str, Field(max_length=1000)] | None, Blank]
Name = Annotated[str, Field(min_length=1, max_length=120)]
Minor = Annotated[int, Field(ge=0, le=MAX_MINOR, description="In the currency's minor unit")]
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
LocalTime = Annotated[
    str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$", description="HH:MM, at the destination")
]
Zone = Annotated[Annotated[str, AfterValidator(_known_zone)] | None, Blank]
Kind = Annotated[str, AfterValidator(_known_kind)]
Minutes = Annotated[int, Field(ge=1, le=rules().activities.max_duration_minutes)]
BookingId = Annotated[Annotated[str, Field(max_length=24)] | None, Blank]
# A record of the same trip, by id: checked there, and not to reveal what an unknown one looks like.
RecordRef = Annotated[Annotated[str, Field(max_length=32)] | None, Blank]
Quantity = Annotated[int, Field(ge=1, le=rules().checklist.max_quantity)]
Category = Annotated[str, AfterValidator(_known_category)]
LocalMoment = Annotated[
    str,
    Field(
        pattern=r"^\d{4}-\d{2}-\d{2}T([01]\d|2[0-3]):[0-5]\d$",
        description="YYYY-MM-DDTHH:MM, local where it happens",
    ),
    AfterValidator(_real_moment),
]
Moment = Annotated[LocalMoment | None, Blank]
Link = Annotated[Annotated[str, Field(max_length=500, pattern=r"^https?://\S+$")] | None, Blank]
# What the person may say of a booking: the app alone says it was extracted or verified.
PersonStatus = Literal["entered", "confirmed"]
Sharers = Annotated[
    list[Annotated[str, Field(min_length=1, max_length=32)]],
    Field(max_length=rules().trips.max_travellers),
    AfterValidator(_each_once),
]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TravelRecorded(BaseModel):
    """What a write answers besides: the event of the trip's history it recorded, so the page can
    undo exactly that one (while it can be undone)."""

    event_id: str | None = Field(
        description="The event this write recorded. None: it recorded none, as it changed nothing "
        "(a tick records none either: only the item's own revisions keep it)"
    )

    @classmethod
    def of_event(cls, event: TripEvent | None) -> Self:
        return cls(event_id=None if event is None else str(event.id))


# ---------------------------------------------------------------------- trips


class DestinationIn(_In):
    name: Name
    country: Name
    timezone: Zone = Field(default=None, description="An IANA time zone, for the trip's today")


class TravellerIn(_In):
    id: Annotated[str | None, Field(max_length=32)] = Field(
        default=None, description="An existing traveller of this trip; left out for a new one"
    )
    name: Name
    passport_country: Text80 = None
    residence_country: Text80 = None
    passport_expiry: dt.date | None = None
    dietary: Text200 = None
    accessibility: Text200 = None


Destinations = Annotated[
    list[DestinationIn], Field(min_length=1, max_length=rules().trips.max_destinations)
]
Travellers = Annotated[list[TravellerIn], Field(max_length=rules().trips.max_travellers)]
Interests = Annotated[
    list[Annotated[str, Field(min_length=1, max_length=40)]],
    Field(max_length=rules().trips.max_interests),
]


class TripIn(_In):
    title: Name
    destinations: Destinations
    departure_city: Text80 = None
    start_date: dt.date
    end_date: dt.date
    trip_type: TripType = TripType.OTHER
    pace: Pace = Pace.BALANCED
    budget_minor: Minor | None = None
    currency: Currency | None = Field(default=None, description="Default: my base currency")
    mode: TripMode = TripMode.STATIC
    interests: Interests = Field(default_factory=list)
    notes: Text1000 = None
    remind_prepare: bool = False
    travellers: Travellers = Field(default_factory=list)


class TripPatch(_In):
    """Only what is sent changes, as of the revision edited: moved on since, the answer is 409."""

    title: Name | None = None
    destinations: Destinations | None = None
    departure_city: Text80 = None
    start_date: dt.date | None = None
    end_date: dt.date | None = None
    trip_type: TripType | None = None
    pace: Pace | None = None
    budget_minor: Minor | None = None
    currency: Currency | None = None
    mode: TripMode | None = None
    interests: Interests | None = None
    notes: Text1000 = None
    remind_prepare: bool | None = None
    travellers: Travellers | None = None
    revision_no: int = Field(ge=1)


class TravelTripOut(BaseModel):
    id: str
    title: str
    destinations: list[Destination]
    departure_city: str | None
    start_date: dt.date
    end_date: dt.date
    trip_type: TripType
    pace: Pace
    budget_minor: int | None
    currency: str
    mode: TripMode
    interests: list[str]
    notes: str | None
    remind_prepare: bool
    travellers: list[Traveller]
    phase: TripPhase = Field(description="Where the trip stands on its own today")
    revision_no: int
    created_at: dt.datetime
    updated_at: dt.datetime


class TravelTripRecorded(TravelTripOut, TravelRecorded):
    """A trip as a write left it, and the event that write recorded."""


class TravelOptions(BaseModel):
    """What the forms offer and check (data/travel.yaml)."""

    activity_kinds: list[str]
    max_trip_days: int
    max_destinations: int
    max_travellers: int
    max_interests: int
    premium_available: bool = Field(description="Whether I may save a trip in Premium mode")
    bags: list[PackingBag]
    max_item_quantity: int
    expense_categories: list[str]


# ---------------------------------------------------------------------- activities


class ActivityFields(_In):
    title: Name
    kind: Kind
    start_time: LocalTime | None = None
    duration_minutes: Minutes | None = None
    location: Text200 = None
    transport: Text200 = None
    notes: Text1000 = None
    estimated_cost_minor: Minor | None = None
    confirmed: bool = Field(default=False, description="False: its details are an estimate")
    booking_id: BookingId = Field(default=None, description="A booking of the same trip")


class TravelActivityIn(ActivityFields):
    day: dt.date


class EditedActivity(ActivityFields):
    """An activity's fields as an edit leaves them: its day moves only by a reschedule."""

    actual_note: Text500 = None


class TravelActivityPatch(_In):
    """Only what is sent changes, as of the revision edited. The day isn't here: a reschedule
    (``/change``) moves it, with what depends on it."""

    title: Name | None = None
    kind: Kind | None = None
    start_time: LocalTime | None = None
    duration_minutes: Minutes | None = None
    location: Text200 = None
    transport: Text200 = None
    notes: Text1000 = None
    estimated_cost_minor: Minor | None = None
    confirmed: bool | None = None
    booking_id: BookingId = None
    actual_note: Text500 = None
    revision_no: int = Field(ge=1)


class TravelReorderIn(_In):
    day: dt.date
    activity_ids: list[str] = Field(
        min_length=1,
        max_length=rules().activities.max_per_trip,
        description="Every activity of that day, each once, in the new order",
    )


class TravelActivityOut(BaseModel):
    id: str
    trip_id: str
    day: dt.date
    start_time: str | None
    duration_minutes: int | None
    title: str
    kind: str
    location: str | None
    transport: str | None
    notes: str | None
    estimated_cost_minor: int | None
    confirmed: bool
    booking_id: str | None
    position: int
    status: ActivityStatus
    actual_note: str | None
    revision_no: int
    created_at: dt.datetime
    updated_at: dt.datetime

    @classmethod
    def of(cls, activity: TripActivity, **extra: Any) -> Self:
        return cls.model_validate(
            {
                **activity.model_dump(),
                "id": str(activity.id),
                "trip_id": str(activity.trip_id),
                **extra,
            }
        )


class TravelActivityRecorded(TravelActivityOut, TravelRecorded):
    """An activity as a write left it, and the event that write recorded: in a reorder's answer,
    every activity of the day carries the reorder's."""


# ---------------------------------------------------------------------- changes and history


class ChangeAction(StrEnum):
    DONE = "done"
    SKIP = "skip"
    REPLACE = "replace"
    RESCHEDULE = "reschedule"


class TravelChangeIn(_In):
    action: ChangeAction
    actual_note: Text500 = Field(default=None, description="What really happened, or why")
    new_activity: ActivityFields | None = Field(
        default=None, description="Replace: what takes its place, on the same day"
    )
    new_day: dt.date | None = Field(default=None, description="Reschedule: the day it moves to")
    new_start_time: LocalTime | None = Field(
        default=None, description="Reschedule: the time it moves to"
    )
    apply: bool = Field(default=False, description="False: nothing is written, only the preview")
    revision_no: int | None = Field(
        default=None, ge=1, description="The revision previewed: moved on since, the answer is 409"
    )

    @model_validator(mode="after")
    def says_what_its_action_needs(self) -> TravelChangeIn:
        if self.action is ChangeAction.REPLACE and self.new_activity is None:
            raise ValueError("a replacement needs new_activity")
        moved = self.new_day is not None or self.new_start_time is not None
        if self.action is ChangeAction.RESCHEDULE and not moved:
            raise ValueError("a reschedule needs new_day or new_start_time")
        return self


class TravelFlag(BaseModel):
    """A record a change doesn't touch but the person should know about."""

    entity: str
    record_id: str
    title: str
    reason: str = Field(
        description="Why it is listed, worded on the app by key: estimate, linked (a booking for "
        "it), completed, edited"
    )
    suggestion_key: str | None = Field(
        default=None,
        description="A checklist item still the suggestion its rule words (a tick keeps it so): "
        "the page words its title by this key, as in what_changed",
    )


def _days_reached(changes: list[RecordChange]) -> list[dt.date]:
    days = {change.day for change in changes if change.day}
    moved = {item.new for change in changes for item in change.fields if item.field == "day"}
    return [dt.date.fromisoformat(day) for day in sorted(days | moved)]


class TravelChangeOut(BaseModel):
    what_changed: list[RecordChange]
    needs_review: list[TravelFlag] = Field(description="Listed, and never changed")
    kept: list[TravelFlag] = Field(description="Left as they are: the person's own work")
    days: list[dt.date] = Field(description="Every day the change reaches")
    event_id: str | None = Field(description="None in a preview")

    @classmethod
    def of(
        cls,
        changes: list[RecordChange],
        needs_review: list[TravelFlag],
        kept: list[TravelFlag],
        event_id: str | None = None,
        **extra: Any,
    ) -> Self:
        return cls(
            what_changed=changes,
            needs_review=needs_review,
            kept=kept,
            days=_days_reached(changes),
            event_id=event_id,
            **extra,
        )


class TravelEventOut(BaseModel):
    id: str
    trip_id: str
    at: dt.datetime
    kind: str
    summary: dict[str, Any] = Field(description="The words for its summary, by name")
    changes: list[RecordChange]
    undoable: bool = Field(description="It can be tried: nothing it touched may have changed since")
    undone_at: dt.datetime | None

    @classmethod
    def of(cls, event: TripEvent) -> TravelEventOut:
        return cls(
            id=str(event.id),
            trip_id=str(event.trip_id),
            at=event.at,
            kind=event.kind,
            summary=event.summary,
            changes=event.changes,
            undoable=bool(event.undo) and event.undone_at is None,
            undone_at=event.undone_at,
        )


# ---------------------------------------------------------------------- checklist and packing


class TravelChecklistIn(_In):
    title: Name
    day: dt.date | None = Field(default=None, description="Tied to an activity: that activity's")
    activity_id: RecordRef = Field(default=None, description="An activity of the same trip")
    traveller_id: RecordRef = Field(default=None, description="A traveller of the same trip")
    state: ChecklistState = ChecklistState.NOT_STARTED
    bag: PackingBag | None = Field(default=None, description="With a bag, it is a packing item")
    quantity: Quantity = 1


class TravelChecklistPatch(_In):
    """Only what is sent changes. A revision sent is checked (moved on since, the answer is 409);
    a tick, which may be replayed from a phone that was offline, can leave it out."""

    title: Name | None = None
    day: dt.date | None = None
    activity_id: RecordRef = None
    traveller_id: RecordRef = None
    state: ChecklistState | None = None
    bag: PackingBag | None = None
    quantity: Quantity | None = None
    revision_no: int | None = Field(default=None, ge=1)


class TravelChecklistOut(BaseModel):
    id: str
    trip_id: str
    day: dt.date | None
    activity_id: str | None
    traveller_id: str | None
    title: str
    state: ChecklistState
    source: ChecklistSource = Field(description="rule: a suggestion, and to be shown as one")
    edited: bool = Field(description="The person has changed it: from now on it is their own work")
    as_suggested: bool = Field(
        description="Still the suggestion as its rule words it (not renamed): shown in the app's "
        "own words for its key, as a suggestion. A tick or a new quantity keeps it so"
    )
    bag: PackingBag | None
    quantity: int
    suggestion_key: str | None = Field(
        description="What suggested it; the words for it on the app are by its last part"
    )
    revision_no: int
    created_at: dt.datetime
    updated_at: dt.datetime

    @classmethod
    def of(cls, item: ChecklistItem, **extra: Any) -> Self:
        return cls.model_validate(
            {
                **item.model_dump(),
                "id": str(item.id),
                "trip_id": str(item.trip_id),
                "as_suggested": item.as_suggested,
                **extra,
            }
        )


class TravelChecklistRecorded(TravelChecklistOut, TravelRecorded):
    """An item as a write left it, and the event that write recorded (none for a tick)."""


class TravelSuggestIn(_In):
    day: dt.date | None = Field(default=None, description="Only for that day's activities")
    packing: bool = Field(
        default=False,
        description="What to pack: what goes in a bag for its activities and, with no day, the "
        "list for the whole trip by its length. False: all that its activities suggest",
    )
    apply: bool = Field(default=False, description="False: nothing is written, only the preview")


class TravelSuggestOut(TravelChangeOut):
    more: bool = Field(description="More are waiting: asking again adds the next ones")


# ---------------------------------------------------------------------- bookings


class BookingFields(_In):
    """A booking as the person enters it: nothing here is looked up."""

    kind: BookingKind
    title: Name
    reference: Text80 = Field(default=None, description="A PNR, a confirmation or policy number")
    starts_at: Moment = None
    ends_at: Moment = None
    location: Text200 = None
    notes: Text1000 = None
    link: Link = Field(default=None, description="An http or https address")

    @model_validator(mode="after")
    def ends_once_it_has_started(self) -> Self:
        if self.starts_at and self.ends_at and self.ends_at < self.starts_at:
            raise ValueError("a booking ends at or after it starts")
        return self


class TravelBookingIn(BookingFields):
    status: PersonStatus = Field(
        default="entered", description="confirmed: I checked it with whoever it is with"
    )


class TravelBookingPatch(_In):
    """Only what is sent changes, as of the revision edited: moved on since, the answer is 409."""

    kind: BookingKind | None = None
    title: Name | None = None
    reference: Text80 = None
    starts_at: Moment = None
    ends_at: Moment = None
    location: Text200 = None
    notes: Text1000 = None
    link: Link = None
    status: PersonStatus | None = None
    revision_no: int = Field(ge=1)


class TravelBookingOut(BaseModel):
    id: str
    trip_id: str
    kind: BookingKind
    title: str
    reference: str | None
    starts_at: str | None = Field(description="YYYY-MM-DDTHH:MM, local where it happens")
    ends_at: str | None
    location: str | None
    notes: str | None
    link: str | None
    status: BookingStatus = Field(description="extracted and verified are only ever the app's")
    revision_no: int
    created_at: dt.datetime
    updated_at: dt.datetime

    @classmethod
    def of(cls, booking: Booking, **extra: Any) -> Self:
        return cls.model_validate(
            {
                **booking.model_dump(),
                "id": str(booking.id),
                "trip_id": str(booking.trip_id),
                **extra,
            }
        )


class TravelBookingRecorded(TravelBookingOut, TravelRecorded):
    """A booking as a write left it, and the event that write recorded."""


# ---------------------------------------------------------------------- expenses and the budget


class TravelExpenseIn(_In):
    kind: ExpenseKind
    category: Category
    title: Name
    amount_minor: Minor
    currency: Currency | None = Field(default=None, description="Default: the trip's currency")
    day: dt.date | None = Field(
        default=None,
        description="When it was paid or is for: converted at that day's rate (today's for a day "
        "still to come, or none)",
    )
    activity_id: RecordRef = Field(default=None, description="An activity of the same trip")
    paid_by: RecordRef = Field(default=None, description="A traveller of the same trip")
    shared_with: Sharers = Field(
        default_factory=list,
        description="Travellers of the same trip, sharing it evenly; none: it is the payer's own",
    )


class TravelExpensePatch(_In):
    """Only what is sent changes, as of the revision edited: moved on since, the answer is 409."""

    kind: ExpenseKind | None = None
    category: Category | None = None
    title: Name | None = None
    amount_minor: Minor | None = None
    currency: Currency | None = None
    day: dt.date | None = None
    activity_id: RecordRef = None
    paid_by: RecordRef = None
    shared_with: Sharers | None = None
    revision_no: int = Field(ge=1)


class TravelExpenseOut(BaseModel):
    id: str
    trip_id: str
    activity_id: str | None
    kind: ExpenseKind
    category: str
    title: str
    amount_minor: int
    currency: str
    day: dt.date | None
    paid_by: str | None
    shared_with: list[str]
    revision_no: int
    created_at: dt.datetime
    updated_at: dt.datetime

    @classmethod
    def of(cls, expense: TripExpense, **extra: Any) -> Self:
        return cls.model_validate(
            {
                **expense.model_dump(),
                "id": str(expense.id),
                "trip_id": str(expense.trip_id),
                **extra,
            }
        )


class TravelExpenseRecorded(TravelExpenseOut, TravelRecorded):
    """An expense as a write left it, and the event that write recorded."""


class TravelCategoryOut(BaseModel):
    category: str
    estimated_minor: int
    actual_minor: int


class TravelUnconvertedOut(BaseModel):
    """An expense with no rate to the trip's currency: counted nowhere, never at a guess."""

    expense_id: str
    title: str
    kind: ExpenseKind
    category: str
    amount_minor: int
    currency: str
    reason: Literal["unavailable", "unsupported"] = Field(
        description="unavailable: no rate could be had now; unsupported: none for that currency"
    )


class TravelBalanceOut(BaseModel):
    traveller_id: str
    paid_minor: int = Field(description="What they really paid, of what is shared")
    share_minor: int = Field(description="Their part of it")
    balance_minor: int = Field(description="Paid less their part: above 0, they are owed")


class TravelTransferOut(BaseModel):
    from_traveller_id: str
    to_traveller_id: str
    amount_minor: int


class TravelBudgetOut(BaseModel):
    """All in the trip's currency, from its expenses alone: an activity's own estimated cost stays
    on the activity, so nothing is counted twice."""

    currency: str
    budget_minor: int | None
    estimated_minor: int
    actual_minor: int
    remaining_minor: int | None = Field(description="The budget less what was really spent")
    categories: list[TravelCategoryOut]
    unconverted: list[TravelUnconvertedOut]
    balances: list[TravelBalanceOut] = Field(description="Every traveller, in the trip's order")
    settlements: list[TravelTransferOut] = Field(description="Who pays whom to even it all up")


# ---------------------------------------------------------------------- overview


class TravelReadiness(BaseModel):
    completed: int
    applicable: int = Field(description="Every item but those no longer applicable")
    ratio: float = Field(description="completed ÷ applicable, from 0 to 1")


class TravelPendingOut(BaseModel):
    checklist: int = Field(description="Checklist items still to do")
    packing: int = Field(description="Packing items still to do")
    bookings: int = Field(description="Bookings not yet confirmed")


class TravelNextAction(BaseModel):
    action: NextAction
    reason: str = Field(description="Why, worded on the app by key: open_tomorrow ...")
    params: dict[str, Any] = Field(
        description="The figures the reason is worded with. prepare_today, prepare_tomorrow and "
        "finish_checklist also carry the day to open (prepare?when=day): finish_checklist's is "
        "the first day holding a checklist item still to do, null when only the whole trip's are"
    )


class TravelOverviewOut(BaseModel):
    trip_id: str
    today: dt.date = Field(description="The trip's own today")
    phase: TripPhase
    days_to_start: int = Field(description="From today to its first day: 0 on it, below once on")
    day_number: int | None = Field(description="Which day of the trip today is, while it is on")
    trip_days: int
    next_activities: list[TravelActivityOut]
    pending: TravelPendingOut
    readiness: TravelReadiness | None = Field(description="None: no tasks yet, so no figure")
    next_action: TravelNextAction


# ---------------------------------------------------------------------- a day's plan


class TravelTransportOut(BaseModel):
    """How the day gets around, as the person wrote it: nothing here is looked up."""

    activity_id: str
    title: str
    kind: str
    start_time: str | None
    location: str | None
    transport: str | None


class TravelPrepareOut(BaseModel):
    day: dt.date
    within_trip: bool = Field(description="False: before or after the trip's dates")
    activities: list[TravelActivityOut]
    checklist: list[TravelChecklistOut]
    bookings: list[TravelBookingOut] = Field(
        description="Those starting or ending that day, and those its planned activities link to"
    )
    transport: list[TravelTransportOut]
    open_days: list[dt.date] = Field(
        default_factory=list,
        description="Every day of the trip still holding an item to do (not started or in "
        "progress, packing ones too), in order: where the day plans are still open",
    )
