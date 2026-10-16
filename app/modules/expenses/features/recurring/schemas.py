from __future__ import annotations

import datetime as dt
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.modules.expenses.domain import CadenceUnit, EntryKind, PaymentMode, RuleMode
from app.modules.expenses.features.entries.schemas import (
    Amount,
    CategoryId,
    Merchant,
    Note,
    Person,
    Tags,
    Title,
)
from app.modules.expenses.features.recurring.detect import Repeat
from app.modules.expenses.features.recurring.models import RecurringRule, RuleSource
from app.modules.expenses.planning import planning

Signature = Annotated[str, Field(min_length=1, max_length=200)]
DaysBefore = Annotated[
    int, Field(ge=0, le=30, description="Days before each due date to remind (bills)")
]


class TemplateFields(BaseModel):
    """The entry each due date adds, or that marking a bill paid adds."""

    kind: EntryKind = EntryKind.EXPENSE
    title: Title
    amount_minor: Amount
    category_id: CategoryId
    merchant: Merchant = None
    payment_mode: PaymentMode | None = None
    for_whom: Person = None
    tags: Tags = []
    note: Note = None


class TemplateIn(TemplateFields):
    # from_attributes: a change is checked with the stored template merged in (``patched``).
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class CadenceFields(BaseModel):
    unit: CadenceUnit
    every: int = Field(default=1, ge=1, le=12, description="Every this many days, weeks ...")
    anchor: dt.date = Field(
        description="A due date (the first): the others follow from it, on the same day of the "
        "month, or the month's last day when it is shorter"
    )


class CadenceIn(CadenceFields):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class RuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template: TemplateIn
    cadence: CadenceIn
    mode: RuleMode
    remind_days_before: DaysBefore = Field(
        default_factory=lambda: planning().recurring.remind_days_before
    )
    active: bool = True
    signature: Signature | None = Field(
        default=None, description="The suggestion this confirms (GET /recurring/suggestions)"
    )


class RulePatch(BaseModel):
    """Only what is sent changes; the template and cadence are sent whole."""

    model_config = ConfigDict(extra="forbid")

    template: TemplateIn | None = None
    cadence: CadenceIn | None = None
    mode: RuleMode | None = None
    remind_days_before: DaysBefore | None = None
    active: bool | None = None


class RuleOut(BaseModel):
    id: str
    template: TemplateFields
    currency: str
    cadence: CadenceFields
    next_run: dt.date = Field(description="The next due date not added yet, or (a bill) not paid")
    mode: RuleMode
    remind_days_before: int
    active: bool
    source: RuleSource
    signature: str | None
    overdue: bool = Field(description="A bill whose due date has passed unpaid")

    @classmethod
    def of(cls, rule: RecurringRule, today: dt.date) -> RuleOut:
        overdue = rule.mode is RuleMode.REMIND and rule.active and rule.next_run < str(today)
        return cls.model_validate({**rule.model_dump(), "id": str(rule.id), "overdue": overdue})


class SuggestionOut(BaseModel):
    """Something in the entries that looks like it repeats, as a rule for it would start."""

    signature: str = Field(description="Sent back to confirm it (POST /recurring) or dismiss it")
    template: TemplateFields
    currency: str
    cadence: CadenceFields = Field(description="Anchored on its next due date, today or later")
    count: int = Field(description="How many times it was seen at this pace")
    last_on: dt.date = Field(description="When it was last seen")

    @classmethod
    def of(cls, repeat: Repeat, next_on: dt.date) -> SuggestionOut:
        latest = repeat.latest
        # The last entry as it was, but at the usual amount and without its own note.
        template = {**latest.model_dump(), "amount_minor": repeat.amount_minor, "note": None}
        return cls.model_validate(
            {
                "signature": repeat.signature,
                "template": template,
                "currency": latest.currency,
                "cadence": {"unit": repeat.unit, "anchor": next_on},
                "count": repeat.count,
                "last_on": repeat.last_on,
            }
        )


class DismissIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signature: Signature


class MarkPaidIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    due_on: dt.date = Field(description="The due date paid: the next one, or one before it")
    amount_minor: Amount | None = Field(default=None, description="Default: the usual amount")
    date: dt.date | None = Field(default=None, description="When it was paid (default today)")
