from __future__ import annotations

import datetime as dt
import re
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    model_validator,
)

from app.modules.expenses.domain import EntryKind, EntrySource, PaymentMode
from app.modules.expenses.features.entries.models import Entry
from app.modules.expenses.units import known_unit
from app.platform import FieldChange, Revision, RevisionAction

MAX_TAGS = 10
MAX_ATTACHMENTS = 5
MAX_QUANTITY = 10**6
MAX_CANDIDATES = 10  # entries checked for duplicates in one request (a confirm card's drafts)


def _upto(max_length: int) -> StringConstraints:
    return StringConstraints(strip_whitespace=True, min_length=1, max_length=max_length)


def _blank_is_none(value: Any) -> Any:
    return (value.strip() or None) if isinstance(value, str) else value


def _clean_tags(tags: list[str]) -> list[str]:
    """Lower-cased and without repeats, in the order given, so "Food" and "food" are one tag."""
    return list(dict.fromkeys(tag.casefold() for tag in tags))


# An empty optional box means "none", not an empty string.
Blank = BeforeValidator(_blank_is_none)
Title = Annotated[str, _upto(120)]
Name = Annotated[str, _upto(60)]  # what a budget or goal is called, or who a loan is with
CategoryId = Annotated[str, _upto(40)]
Tag = Annotated[str, _upto(30)]
Merchant = Annotated[Annotated[str, _upto(80)] | None, Blank]
Person = Annotated[Annotated[str, _upto(60)] | None, Blank]
Label = Annotated[Annotated[str, _upto(60)] | None, Blank]
Note = Annotated[Annotated[str, _upto(1000)] | None, Blank]
UnitCode = Annotated[Annotated[str, AfterValidator(known_unit)] | None, Blank]
Tags = Annotated[list[Tag], Field(max_length=MAX_TAGS), AfterValidator(_clean_tags)]
Amount = Annotated[int, Field(gt=0, le=10**13, description="In the currency's minor unit")]


class AttachmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    label: Label = None


class EntryFields(BaseModel):
    kind: EntryKind = EntryKind.EXPENSE
    title: Title
    amount_minor: Amount
    currency: str | None = Field(
        default=None, pattern=r"^[A-Z]{3}$", description="Default: my base currency"
    )
    quantity: float | None = Field(
        default=None, gt=0, le=MAX_QUANTITY, description="How much was bought, in `unit`"
    )
    unit: UnitCode = Field(default=None, description="A code from GET /units")
    category_id: CategoryId
    date: dt.date = Field(description="The calendar day it happened")
    time: dt.time | None = None
    merchant: Merchant = None
    payment_mode: PaymentMode | None = None
    for_whom: Person = None
    tags: Tags = []
    note: Note = None
    attachments: Annotated[list[AttachmentIn], Field(max_length=MAX_ATTACHMENTS)] = []

    @model_validator(mode="after")
    def quantity_with_unit(self) -> EntryFields:
        if (self.quantity is None) != (self.unit is None):
            raise ValueError("give the quantity and its unit together")
        return self


class EntryIn(EntryFields):
    model_config = ConfigDict(extra="forbid")


class EntryUpdate(EntryIn):
    """The whole entry as edited, plus the revision that was edited: if the entry has moved
    on since, the answer is 409 rather than one change silently replacing the other."""

    revision_no: int = Field(ge=1)


class EntryOut(EntryFields):
    id: str
    currency: str
    source: EntrySource
    recurring_id: str | None = Field(
        default=None, description="The repeating rule that added it, or whose bill it paid"
    )
    revision_no: int
    deleted: bool
    created_at: dt.datetime
    updated_at: dt.datetime

    @classmethod
    def of(cls, entry: Entry) -> EntryOut:
        return cls.model_validate(
            {**entry.model_dump(), "id": str(entry.id), "deleted": entry.deleted_at is not None}
        )


class RevisionOut(BaseModel):
    revision_no: int
    action: RevisionAction
    changed_at: dt.datetime
    source: str
    changes: list[FieldChange]
    snapshot: dict[str, Any]

    @classmethod
    def of(cls, revision: Revision) -> RevisionOut:
        return cls.model_validate(revision.model_dump())


class RestoreIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision_no: int = Field(ge=1)


class CurrencyTotal(BaseModel):
    """Totals are per currency: amounts in different currencies are never added together."""

    currency: str
    expense_minor: int = 0
    income_minor: int = 0
    count: int = 0


class SummaryOut(BaseModel):
    totals: list[CurrencyTotal]


class FacetsOut(BaseModel):
    """What the user has typed before, for suggestions while they type."""

    tags: list[str]
    for_whom: list[str]
    merchants: list[str]


class EntryFilters(BaseModel):
    """Query parameters shared by the list and its summary."""

    kind: EntryKind | None = None
    start: dt.date | None = Field(default=None, alias="from", description="First day")
    end: dt.date | None = Field(default=None, alias="to", description="Last day, inclusive")
    category_id: str | None = Field(default=None, max_length=40)
    tag: str | None = Field(default=None, max_length=30)
    for_whom: str | None = Field(default=None, max_length=60)
    merchant: str | None = Field(default=None, max_length=80, description="Exactly this place")
    payment_mode: PaymentMode | None = None
    q: str | None = Field(default=None, max_length=80, description="In title, merchant or note")
    min_amount: int | None = Field(default=None, ge=0, description="Minor units")
    max_amount: int | None = Field(default=None, ge=0, description="Minor units")
    deleted: bool = Field(default=False, description="true = only deleted entries")

    def criteria(self) -> dict[str, Any]:
        found: dict[str, Any] = self.model_dump(
            mode="json",
            exclude_none=True,
            include={"kind", "category_id", "for_whom", "merchant", "payment_mode"},
        )
        if self.tag:
            found["tags"] = self.tag.casefold()
        if days := _between(self.start, self.end):
            found["date"] = days
        if amounts := _bounds(self.min_amount, self.max_amount):
            found["amount_minor"] = amounts
        if self.q and self.q.strip():
            pattern = {"$regex": re.escape(self.q.strip()), "$options": "i"}
            found["$or"] = [{name: pattern} for name in ("title", "merchant", "note")]
        if self.deleted:
            found["deleted_at"] = {"$ne": None}
        return found


class EntryListQuery(EntryFilters):
    """The list's whole query string. FastAPI reads a model from the query only when it is
    the endpoint's one query parameter, so paging and sorting live here too."""

    page: int = Field(default=1, ge=1, description="1-based page number")
    page_size: int | None = Field(default=None, ge=1, description="Items per page")
    sort: str | None = Field(default=None, description="e.g. -date,amount_minor")


class DuplicateCandidate(BaseModel):
    """An entry about to be saved, as far as telling it from saved ones needs."""

    model_config = ConfigDict(extra="forbid")

    kind: EntryKind = EntryKind.EXPENSE
    title: Title
    amount_minor: int = Field(gt=0, le=10**13, description="In the base currency's minor unit")
    date: dt.date
    merchant: Merchant = None
    exclude_id: str | None = Field(
        default=None, max_length=40, description="The entry being edited, never its own match"
    )


class DuplicatesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: Annotated[list[DuplicateCandidate], Field(min_length=1, max_length=MAX_CANDIDATES)]


class DuplicateResult(BaseModel):
    index: int = Field(description="The candidate's place in the request")
    matches: list[EntryOut]


class DuplicatesOut(BaseModel):
    """One result per candidate, in the order asked. Advice only: saving is never refused."""

    results: list[DuplicateResult]


def _bounds(low: Any, high: Any) -> dict[str, Any]:
    return {op: value for op, value in (("$gte", low), ("$lte", high)) if value is not None}


def _between(start: dt.date | None, end: dt.date | None) -> dict[str, Any]:
    return _bounds(start and start.isoformat(), end and end.isoformat())


def on_days(start: dt.date, end: dt.date) -> dict[str, Any]:
    """Filter for entries dated within the days, both inclusive."""
    return {"date": _between(start, end)}
