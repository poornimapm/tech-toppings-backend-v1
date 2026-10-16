from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.modules.expenses.domain import EntryKind
from app.modules.expenses.features.analytics.service import tuning

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]


class Metric(StrEnum):
    TOTAL = "total"  # money, added up
    COUNT = "count"  # how many entries
    AVERAGE = "average"  # money, per entry
    LARGEST = "largest"  # money, the biggest entry


class GroupBy(StrEnum):
    NONE = "none"
    CATEGORY = "category"
    MERCHANT = "merchant"
    PERSON = "person"
    TAG = "tag"
    DAY = "day"
    MONTH = "month"
    KIND = "kind"
    PAYMENT_MODE = "payment_mode"


class SortOrder(StrEnum):
    VALUE_DESC = "value_desc"  # biggest first
    VALUE_ASC = "value_asc"
    KEY = "key"  # by the group (days and months in order)


class ExpenseQuery(BaseModel):
    """A question about the user's entries, as data: what to work out, of which entries, by
    what. It is compiled from a whitelist (compiler.py), so it can only ever read the user's own
    entries, in fixed shapes."""

    model_config = ConfigDict(extra="forbid")

    metric: Metric = Metric.TOTAL
    kind: EntryKind | None = Field(
        default=EntryKind.EXPENSE, description="None for both (then group by kind)"
    )
    group_by: GroupBy = GroupBy.NONE
    start: dt.date | None = Field(default=None, description="First day")
    end: dt.date | None = Field(default=None, description="Last day, inclusive")
    category_ids: Annotated[list[str], Field(max_length=tuning().query.max_categories)] = []
    merchant: Text | None = Field(default=None, description="This place exactly (any case)")
    person: Text | None = Field(default=None, description="For this person exactly (any case)")
    tag: Text | None = None
    text: Text | None = Field(default=None, description="Words in the title, place or note")
    min_amount_minor: int | None = Field(default=None, ge=0)
    max_amount_minor: int | None = Field(default=None, ge=0)
    sort: SortOrder = SortOrder.VALUE_DESC
    limit: int = Field(default=10, ge=1, le=tuning().query.max_rows)

    @model_validator(mode="after")
    def days_in_order(self) -> ExpenseQuery:
        if self.start and self.end and self.end < self.start:
            raise ValueError("the last day can't be before the first")
        return self


class QueryRow(BaseModel):
    key: str | None = Field(description="The group (None: not said, or no grouping)")
    value: int = Field(description="The metric: minor units for money, else a count")
    count: int = Field(description="Entries in the group")


class QueryResult(BaseModel):
    metric: Metric
    group_by: GroupBy
    currency: str
    money: bool = Field(description="value is an amount in minor units (else a count)")
    rows: list[QueryRow]
