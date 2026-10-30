"""What the routes take and give. Named after the module (``Labor...``), so its API types stay
its own."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field

from app.modules.labor.models import (
    AttendanceStatus,
    Gender,
    LaborAttendance,
    LaborAuditEntry,
    LaborPayment,
    LaborProject,
    LaborWageChange,
    LaborWorker,
    PaymentMethod,
    ProjectStatus,
)
from app.modules.labor.rules import rules

MAX_MINOR = 10**13  # the largest amount taken, as everywhere in the app


def _blank_is_none(value: object) -> object:
    return None if isinstance(value, str) and not value.strip() else value


Blank = BeforeValidator(_blank_is_none)  # an emptied field is no value


# Optional text up to a length (the length is the text's: none is no text).
Text8 = Annotated[Annotated[str, Field(max_length=8)] | None, Blank]
Text12 = Annotated[Annotated[str, Field(max_length=12)] | None, Blank]
Text40 = Annotated[Annotated[str, Field(max_length=40)] | None, Blank]
Text80 = Annotated[Annotated[str, Field(max_length=80)] | None, Blank]
Text200 = Annotated[Annotated[str, Field(max_length=200)] | None, Blank]
Text300 = Annotated[Annotated[str, Field(max_length=300)] | None, Blank]
Text500 = Annotated[Annotated[str, Field(max_length=500)] | None, Blank]
Name = Annotated[str, Field(min_length=1, max_length=80)]
Minor = Annotated[int, Field(ge=0, le=MAX_MINOR, description="In the currency's minor unit")]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ---------------------------------------------------------------------- projects


class ProjectIn(_In):
    name: Name
    kind: Text40 = None
    description: Text300 = None
    icon: Text8 = None
    started_on: dt.date | None = Field(default=None, description="Default today")
    notes: Text500 = None


class StatusIn(_In):
    note: Text300 = None


class RatesIn(_In):
    by_gender: bool = False
    default_minor: Minor = 0
    female_minor: Minor = 0
    male_minor: Minor = 0
    note: Text300 = None


class RatesOut(BaseModel):
    by_gender: bool
    default_minor: int
    female_minor: int
    male_minor: int


class LaborProjectOut(BaseModel):
    id: str
    name: str
    kind: str | None
    description: str | None
    icon: str | None
    started_on: dt.date
    notes: str | None
    status: ProjectStatus
    closed_at: dt.datetime | None
    currency: str
    rates: RatesOut
    created_at: dt.datetime
    updated_at: dt.datetime

    @classmethod
    def of(cls, project: LaborProject) -> LaborProjectOut:
        return cls(
            **project.model_dump(
                include={"name", "kind", "description", "icon", "notes", "status", "closed_at"}
            ),
            id=str(project.id),
            started_on=dt.date.fromisoformat(project.started_on),
            currency=project.currency,
            rates=RatesOut(**project.rates.model_dump()),
            created_at=project.created_at,
            updated_at=project.updated_at,
        )


# ---------------------------------------------------------------------- workers


class WorkerIn(_In):
    name: Name
    code: Text12 = None
    gender: Gender | None = None
    phone: Annotated[Annotated[str, Field(pattern=r"^\+?[0-9 -]{6,20}$")] | None, Blank] = None
    notes: Text500 = None
    daily_wage_minor: Minor | None = Field(
        default=None, description="Default: the project's rate (by gender where it uses that)"
    )


class WageIn(_In):
    daily_wage_minor: Minor
    effective_on: dt.date | None = Field(default=None, description="Default today")
    reason: Text300 = None


class ActiveIn(_In):
    active: bool
    note: Text300 = None


class LaborWorkerOut(BaseModel):
    id: str
    project_id: str
    name: str
    code: str
    gender: Gender | None
    phone: str | None
    daily_wage_minor: int
    active: bool
    notes: str | None
    has_photo: bool
    has_id_card: bool
    created_at: dt.datetime
    updated_at: dt.datetime

    @classmethod
    def of(cls, worker: LaborWorker) -> LaborWorkerOut:
        return cls(
            **worker.model_dump(
                include={"name", "code", "gender", "phone", "daily_wage_minor", "active", "notes"}
            ),
            id=str(worker.id),
            project_id=str(worker.project_id),
            has_photo=worker.photo_id is not None,
            has_id_card=worker.id_card_id is not None,
            created_at=worker.created_at,
            updated_at=worker.updated_at,
        )


class LaborWageOut(BaseModel):
    from_minor: int | None
    to_minor: int
    effective_on: dt.date
    reason: str | None
    created_at: dt.datetime

    @classmethod
    def of(cls, change: LaborWageChange) -> LaborWageOut:
        return cls(
            from_minor=change.from_minor,
            to_minor=change.to_minor,
            effective_on=dt.date.fromisoformat(change.effective_on),
            reason=change.reason,
            created_at=change.created_at,
        )


# ---------------------------------------------------------------------- attendance


class MarkIn(_In):
    worker_id: str
    status: AttendanceStatus
    note: Text200 = None
    expected: Literal["full", "half", "absent", "unmarked"] | None = Field(
        default=None,
        description=(
            "The mark the sender last saw (a tap kept offline and sent later): one changed "
            "since to something else is refused (LABOR_MARK_CHANGED), never overwritten"
        ),
    )


class MarksIn(_In):
    marks: list[MarkIn] = Field(min_length=1, max_length=500)


# ---------------------------------------------------------------------- marks said or typed

DAY_TEXT = r"^\d{4}-\d{2}-\d{2}$"  # a day as the model writes it; checked again when saved


def _real_day(value: str) -> str:
    """A day that exists ("2026-09-31" doesn't): a ValidationError the model is told about."""
    dt.date.fromisoformat(value)
    return value


DayText = Annotated[str, Field(pattern=DAY_TEXT), AfterValidator(_real_day)]
DraftStatus = Literal["full", "half", "absent"]


class DraftMarkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_id: Annotated[
        str | None,
        Field(description="The worker's id from labor_day; left out when no one matched"),
    ] = None
    name: Annotated[
        str, Field(min_length=1, max_length=80, description="Their name, or the words heard")
    ]
    status: Annotated[DraftStatus | None, Field(description="Their mark; left out if not said")] = (
        None
    )
    uncertain: Annotated[
        list[Literal["worker", "status"]], Field(description="What was guessed: who, or which mark")
    ] = []


class AttendanceDraftIn(BaseModel):
    """A day's marks as said. Leave out what wasn't said."""

    model_config = ConfigDict(extra="forbid")

    project_id: Annotated[str | None, Field(description="The project's id from labor_day")] = None
    day: Annotated[DayText | None, Field(description="YYYY-MM-DD; never later than today")] = None
    marks: Annotated[list[DraftMarkIn], Field(max_length=500)] = []
    rest: Annotated[
        DraftStatus | None, Field(description="The mark for every worker not named, if said")
    ] = None
    uncertain: Annotated[list[Literal["day", "rest"]], Field(description="What was guessed")] = []


class ReadMarksIn(_In):
    text: str = Field(min_length=1, max_length=rules().reading.max_chars)
    input_type: Literal["text", "speech"] = "text"
    locale: str | None = Field(
        default=None, max_length=10, pattern=r"^[a-z]{2}(-[A-Za-z]{2})?$", description="en, ta, hi"
    )


class MarksReadOut(BaseModel):
    """What the mark-attendance skill read, as a draft for the page; never saved."""

    status: str
    mode: str | None
    reason: str | None
    message: str | None = Field(description="Agent mode: its sentence")
    message_code: str | None = Field(description="Static mode: worded on the app")
    message_params: dict[str, Any]
    draft: AttendanceDraftIn | None


class LaborMarkOut(BaseModel):
    worker_id: str
    status: AttendanceStatus
    units: float
    rate_minor: int = Field(description="The wage it was recorded at")
    amount_minor: int
    note: str | None

    @classmethod
    def of(cls, mark: LaborAttendance) -> LaborMarkOut:
        return cls(
            **mark.model_dump(include={"status", "units", "rate_minor", "amount_minor", "note"}),
            worker_id=str(mark.worker_id),
        )


class LaborDayOut(BaseModel):
    day: dt.date
    workers: list[LaborWorkerOut]
    marks: list[LaborMarkOut]
    counts: dict[str, int] = Field(description="full, half, absent and unmarked")
    total_minor: int = Field(description="What the day's marks earned")


# ---------------------------------------------------------------------- payments


class PaymentIn(_In):
    worker_id: str
    amount_minor: Annotated[int, Field(gt=0, le=MAX_MINOR)]
    day: dt.date | None = Field(default=None, description="Default today")
    method: PaymentMethod | None = None
    reference: Text80 = None
    note: Text300 = None


class VoidIn(_In):
    reason: Annotated[str, Field(min_length=1, max_length=300)]


class LaborPaymentOut(BaseModel):
    id: str
    worker_id: str
    day: dt.date
    amount_minor: int
    method: PaymentMethod | None
    reference: str | None
    note: str | None
    voided_at: dt.datetime | None
    void_reason: str | None
    created_at: dt.datetime

    @classmethod
    def of(cls, payment: LaborPayment) -> LaborPaymentOut:
        return cls(
            **payment.model_dump(
                include={"amount_minor", "method", "reference", "note", "voided_at", "void_reason"}
            ),
            id=str(payment.id),
            worker_id=str(payment.worker_id),
            day=dt.date.fromisoformat(payment.day),
            created_at=payment.created_at,
        )


# ---------------------------------------------------------------------- reports and audit


class LaborTotalsOut(BaseModel):
    full: int
    half: int
    absent: int
    days: float
    earned_minor: int
    paid_minor: int
    owed_minor: int


class LaborPeriodOut(LaborTotalsOut):
    start: dt.date


class LaborWorkerTotalsOut(LaborTotalsOut):
    worker_id: str
    code: str
    name: str
    earned_to_date_minor: int = Field(description="Earned on every day up to the end day")
    paid_to_date_minor: int = Field(description="Paid (not voided) on every day up to the end day")
    owed_to_date_minor: int = Field(description="Earned less paid, up to the end day")


class LaborReportOut(BaseModel):
    start: dt.date
    end: dt.date
    grouping: str
    periods: list[LaborPeriodOut]
    workers: list[LaborWorkerTotalsOut]
    total: LaborTotalsOut


class LaborChangeOut(BaseModel):
    field: str
    before: Any
    after: Any


class LaborAuditOut(BaseModel):
    id: str
    at: dt.datetime
    actor_name: str
    actor_role: str
    project_id: str | None
    entity: str
    entity_id: str
    action: str
    changes: list[LaborChangeOut]
    note: str | None
    labels: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Words for the record ids the entry holds: the worker a change names, the worker "
            "an entry is about, a payment's worker (by the payment's id)"
        ),
    )
    currency: str | None = Field(default=None, description="The project's: its amounts are in it")

    @classmethod
    def of(
        cls,
        entry: LaborAuditEntry,
        names: Mapping[str, str] | None = None,
        currency: str | None = None,
    ) -> LaborAuditOut:
        held = {
            value
            for change in entry.changes
            for value in (change.before, change.after)
            if isinstance(value, str)
        } | {entry.entity_id}
        return cls(
            labels={key: label for key, label in (names or {}).items() if key in held},
            currency=currency,
            id=str(entry.id),
            at=entry.at,
            actor_name=entry.actor_name,
            actor_role=entry.actor_role,
            project_id=str(entry.project_id) if entry.project_id else None,
            entity=entry.entity,
            entity_id=entry.entity_id,
            action=entry.action,
            changes=[LaborChangeOut(**change.model_dump()) for change in entry.changes],
            note=entry.note,
        )


class LaborDayMarkOut(LaborMarkOut):
    day: dt.date

    @classmethod
    def of(cls, mark: LaborAttendance) -> LaborDayMarkOut:
        return cls(**LaborMarkOut.of(mark).model_dump(), day=dt.date.fromisoformat(mark.day))


class LaborWorkerHistoryOut(BaseModel):
    """A worker's days and payments between two days, newest first, and what they add up to."""

    worker: LaborWorkerOut
    start: dt.date
    end: dt.date
    days: list[LaborDayMarkOut]
    payments: list[LaborPaymentOut] = Field(description="Voided ones too, marked")
    earned_minor: int
    paid_minor: int = Field(description="Voided payments left out")


class LaborOptions(BaseModel):
    """What the forms offer and check (data/labor.yaml)."""

    kinds: list[str]
    genders: list[str]
    methods: list[str]
    units: dict[str, float]
    max_daily_minor: int
    photo_max_bytes: int
    id_max_bytes: int
    photo_side_px: int
    id_side_px: int
    max_report_days: int
