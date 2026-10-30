"""What the routes take and give. Named after the module, so its API types stay its own."""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from app.modules.receipts.models import ReceiptsItem
from app.modules.receipts.warranty import WarrantyStatus, rules, status_on

TEXT_MAX = 200
MAX_MINOR = 10**13  # the largest amount taken, as expenses takes it


def _blank_is_none(value: object) -> object:
    return None if isinstance(value, str) and not value.strip() else value


Blank = BeforeValidator(_blank_is_none)  # an emptied field is no value
# The limit is the text's own: an emptied field (None) has none to meet.
Shop = Annotated[
    Annotated[str, Field(max_length=80)] | None, Blank, Field(description="Where it was bought")
]
BillNumber = Annotated[
    Annotated[str, Field(max_length=40)] | None,
    Blank,
    Field(description="The bill's or invoice's number"),
]
SerialNumber = Annotated[
    Annotated[str, Field(max_length=60)] | None,
    Blank,
    Field(description="The thing's serial number or IMEI, for a warranty claim"),
]
Note = Annotated[
    Annotated[str, Field(max_length=500)] | None, Blank, Field(description="Anything else to keep")
]


class ReceiptsItemIn(BaseModel):
    """A receipt as the form sends it: everything, each time (a change replaces the record)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    text: str = Field(min_length=1, max_length=TEXT_MAX, description="What was bought")
    shop: Shop = None
    bought_on: dt.date | None = Field(default=None, description="Default today; never later")
    amount_minor: int | None = Field(
        default=None, gt=0, le=MAX_MINOR, description="In the base currency's minor unit"
    )
    bill_number: BillNumber = None
    serial_number: SerialNumber = None
    warranty_ends_on: dt.date | None = Field(
        default=None, description="The warranty's last day; none for no warranty"
    )
    remind_days_before: int | None = Field(
        default=None,
        ge=0,
        le=rules().warranty.max_remind_days,
        description="Days before the warranty ends to remind (default from the module's data)",
    )
    note: Note = None


class ReceiptsItemOut(BaseModel):
    id: str
    text: str
    shop: str | None
    bought_on: dt.date
    amount_minor: int | None
    currency: str
    bill_number: str | None
    serial_number: str | None
    warranty_ends_on: dt.date | None
    remind_days_before: int
    warranty: WarrantyStatus = Field(description="Where the warranty stands today")
    note: str | None
    created_at: dt.datetime
    updated_at: dt.datetime

    @classmethod
    def of(cls, item: ReceiptsItem, today: dt.date) -> ReceiptsItemOut:
        return cls(
            id=str(item.id),
            text=item.text,
            shop=item.shop,
            bought_on=dt.date.fromisoformat(item.bought_on),
            amount_minor=item.amount_minor,
            currency=item.currency,
            bill_number=item.bill_number,
            serial_number=item.serial_number,
            warranty_ends_on=(
                dt.date.fromisoformat(item.warranty_ends_on) if item.warranty_ends_on else None
            ),
            remind_days_before=item.remind_days_before,
            warranty=status_on(today, item.warranty_ends_on, item.remind_on),
            note=item.note,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )


class ReceiptsOptions(BaseModel):
    """What the form offers and starts with (data/warranty.yaml)."""

    remind_days_before: int
    max_remind_days: int
    max_months: int
    quick_months: list[int]
