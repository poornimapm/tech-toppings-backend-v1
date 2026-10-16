from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, Field
from pymongo import ASCENDING, IndexModel

from app.modules.expenses.domain import MODULE, EntryKind, PaymentMode, RuleMode
from app.modules.expenses.features.recurring.cadence import Cadence
from app.platform import OwnedDocument, ScopedRepository


class RuleSource(StrEnum):
    MANUAL = "manual"  # set up by the user
    DETECTED = "detected"  # a suggestion the user confirmed


class RuleTemplate(BaseModel):
    """The entry each due date adds, or that marking a bill paid adds."""

    kind: EntryKind
    title: str
    amount_minor: int
    category_id: str
    merchant: str | None = None
    payment_mode: PaymentMode | None = None
    for_whom: str | None = None
    tags: list[str] = Field(default_factory=list)
    note: str | None = None


# One rule per suggestion confirmed: confirming it again finds that rule.
SIGNATURE_INDEX = IndexModel(
    [("user_id", ASCENDING), ("signature", ASCENDING)],
    unique=True,
    partialFilterExpression={"signature": {"$type": "string"}},
    name="user_signature_unique",
)


class RecurringRule(OwnedDocument):
    """Something that repeats (rent, a salary, a subscription): its entries are added on their
    days, or, for a bill, the user is reminded and marks each one paid (U3)."""

    MODULE_KEY: ClassVar[str] = MODULE

    template: RuleTemplate
    currency: str
    cadence: Cadence
    # YYYY-MM-DD: the next due date not added yet, or (a bill) not paid yet. A bill stays due
    # until it is marked paid.
    next_run: str
    mode: RuleMode
    remind_days_before: int
    active: bool = True
    source: RuleSource = RuleSource.MANUAL
    signature: str | None = None  # the suggestion it was confirmed from

    class Settings:
        name = "expense_recurring_rules"
        indexes = [  # noqa: RUF012 - Beanie reads this class attribute
            IndexModel([("user_id", ASCENDING), ("next_run", ASCENDING)], name="user_next_run"),
            SIGNATURE_INDEX,
        ]


class RecurringSuggestion(OwnedDocument):
    """A suggestion the user said is not regular. Suggestions themselves are found afresh from
    the entries each time; only these answers are kept."""

    MODULE_KEY: ClassVar[str] = MODULE

    signature: str
    dismissed_at: dt.datetime

    class Settings:
        name = "expense_recurring_suggestions"
        indexes = [SIGNATURE_INDEX]  # noqa: RUF012 - Beanie reads this class attribute


class RuleRepository(ScopedRepository[RecurringRule]):
    model: ClassVar[type[OwnedDocument]] = RecurringRule


class DismissedRepository(ScopedRepository[RecurringSuggestion]):
    model: ClassVar[type[OwnedDocument]] = RecurringSuggestion
