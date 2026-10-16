from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.expenses.features.entries.schemas import Name, Note
from app.modules.expenses.features.ious.models import Iou, IouDirection
from app.modules.expenses.ledger import MAX_MINOR, LedgerAmount

Amount = Annotated[int, Field(gt=0, le=MAX_MINOR, description="In the base currency's minor unit")]


class IouState(StrEnum):
    OPEN = "open"  # not paid back in full yet
    SETTLED = "settled"
    ALL = "all"


class IouFields(BaseModel):
    counterparty: Name = Field(description="Who the money was lent to or borrowed from")
    direction: IouDirection
    amount_minor: Amount
    date: dt.date | None = Field(default=None, description="When it was lent (default today)")
    due_on: dt.date | None = Field(default=None, description="When it should be paid back")
    note: Note = None

    @model_validator(mode="after")
    def due_after_lent(self) -> IouFields:
        if self.due_on and self.date and self.due_on < self.date:
            raise ValueError("a loan can't be due before it was given")
        return self


class IouIn(IouFields):
    model_config = ConfigDict(extra="forbid")


class IouPatch(BaseModel):
    """Only what is sent changes (``null`` clears the due date or note), plus the revision that
    was edited: if the loan has moved on since (a payment came in), the answer is 409."""

    model_config = ConfigDict(extra="forbid")

    counterparty: Name | None = None
    direction: IouDirection | None = None
    amount_minor: Amount | None = None
    date: dt.date | None = None
    due_on: dt.date | None = None
    note: Note = None
    revision_no: int = Field(ge=1)


class SettlementIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount_minor: Annotated[
        LedgerAmount, Field(description="Paid back; negative to correct a payment")
    ]
    date: dt.date | None = Field(
        default=None, description="Default today; from the day of the loan to today"
    )
    note: Note = None


class SettlementOut(BaseModel):
    key: str
    amount_minor: int
    date: dt.date
    note: str | None


class IouOut(IouFields):
    id: str
    currency: str
    date: dt.date
    counterparty_key: str
    settlements: list[SettlementOut]
    settled_minor: int = Field(description="Paid back so far")
    outstanding_minor: int = Field(description="Still to be paid back")
    settled_on: dt.date | None = Field(description="The day it was paid back in full")
    revision_no: int

    @classmethod
    def of(cls, iou: Iou) -> IouOut:
        outstanding = iou.amount_minor - iou.settled_minor
        return cls.model_validate(
            {**iou.model_dump(), "id": str(iou.id), "outstanding_minor": outstanding}
        )


class PersonBalance(BaseModel):
    """Where the user stands with one person, over their loans in the base currency."""

    name: str = Field(description="As last written")
    key: str = Field(description="The name as compared: the `person` filter of the list")
    owed_to_me_minor: int
    i_owe_minor: int
    net_minor: int = Field(description="Owed to me less what I owe; negative when I owe more")
    open: int = Field(description="Loans with them not yet paid back in full")
    next_due_on: dt.date | None = Field(description="The soonest due date of those")
    overdue: bool = Field(description="One of those was due before today")


class IouSummary(BaseModel):
    currency: str
    owed_to_me_minor: int
    i_owe_minor: int
    people: list[PersonBalance] = Field(
        description="People with open loans first (soonest due first), then the settled ones"
    )
