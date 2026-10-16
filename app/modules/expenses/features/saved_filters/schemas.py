from __future__ import annotations

import datetime as dt
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.expenses.features.entries.schemas import Merchant, Name, Person
from app.modules.expenses.features.saved_filters.models import SavedFilter


class ViewShow(StrEnum):
    ALL = "all"
    EXPENSE = "expense"
    INCOME = "income"
    DELETED = "deleted"


class ViewPeriod(StrEnum):
    """Kept by name, so a view of this month shows this month whenever it is opened."""

    THIS_MONTH = "this_month"
    LAST_MONTH = "last_month"
    LAST_90_DAYS = "last_90_days"
    ALL_TIME = "all_time"


class SavedView(BaseModel):
    """The entries list's filters as the user set them: a period by name, or the days a chart
    or budget chose (``from``/``to``), and what else narrows the list."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    show: ViewShow = ViewShow.ALL
    period: ViewPeriod | None = Field(
        default=None, description="Default this_month; leave out when giving from/to"
    )
    start: dt.date | None = Field(default=None, alias="from", description="First day")
    end: dt.date | None = Field(default=None, alias="to", description="Last day, inclusive")
    q: str | None = Field(default=None, max_length=80, description="In title, merchant or note")
    category_id: str | None = Field(default=None, max_length=40)
    tag: str | None = Field(default=None, max_length=30)
    for_whom: Person = None
    merchant: Merchant = None

    @model_validator(mode="after")
    def period_or_days(self) -> SavedView:
        if self.start or self.end:
            if self.period is not None:
                raise ValueError("a view keeps a period or days (from, to), not both")
            if self.start and self.end and self.end < self.start:
                raise ValueError("the last day can't be before the first")
        elif self.period is None:
            self.period = ViewPeriod.THIS_MONTH
        if self.tag:
            self.tag = self.tag.casefold()  # as entries keep their tags
        if self.q is not None:
            self.q = self.q.strip() or None
        return self


class SavedFilterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    view: SavedView


class SavedFilterPatch(BaseModel):
    """Only what is sent changes: a new name, or the list's filters as they are now."""

    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    view: SavedView | None = None


class SavedFilterOut(BaseModel):
    id: str
    name: str
    view: SavedView
    updated_at: dt.datetime

    @classmethod
    def of(cls, saved: SavedFilter) -> SavedFilterOut:
        return cls(
            id=str(saved.id),
            name=saved.name,
            view=SavedView.model_validate(saved.view),
            updated_at=saved.updated_at,
        )
