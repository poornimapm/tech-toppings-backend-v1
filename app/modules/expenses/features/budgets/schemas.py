from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from app.modules.expenses.features.budgets.models import Budget, BudgetPeriod, BudgetScope
from app.modules.expenses.features.entries.schemas import CategoryId, Name, Tag
from app.modules.expenses.planning import planning

Limit = Annotated[int, Field(gt=0, le=10**13, description="In the base currency's minor unit")]
Threshold = Annotated[
    float, Field(ge=0, le=1, description="Warn once at this share of the limit; 0 = only when over")
]
# Entries keep their tags lower-cased, so a budget's tag is compared the same way.
BudgetTag = Annotated[Tag, AfterValidator(str.casefold)]

# The field each scope is about; the other must be left out.
SCOPE_FIELDS = {
    BudgetScope.OVERALL: set[str](),
    BudgetScope.CATEGORY: {"category_id"},
    BudgetScope.TAG: {"tag"},
}


class BudgetFields(BaseModel):
    name: Name
    scope: BudgetScope
    category_id: CategoryId | None = None
    tag: BudgetTag | None = None
    period: BudgetPeriod
    start: dt.date | None = Field(default=None, description="First day (custom budgets only)")
    end: dt.date | None = Field(default=None, description="Last day, inclusive (custom only)")
    limit_minor: Limit
    alert_threshold: Threshold = Field(default_factory=lambda: planning().budgets.alert_threshold)

    @model_validator(mode="after")
    def fits_scope_and_period(self) -> BudgetFields:
        given = {name for name in ("category_id", "tag") if getattr(self, name) is not None}
        if given != SCOPE_FIELDS[self.scope]:
            raise ValueError("a category budget takes category_id, a tag budget tag, else neither")
        dated = {name for name in ("start", "end") if getattr(self, name) is not None}
        if dated != ({"start", "end"} if self.period is BudgetPeriod.CUSTOM else set()):
            raise ValueError("a custom budget takes start and end; a weekly or monthly one neither")
        if self.start and self.end and self.end < self.start:
            raise ValueError("end can't be before start")
        return self


class BudgetIn(BudgetFields):
    model_config = ConfigDict(extra="forbid")


class BudgetPatch(BaseModel):
    """Only what is sent changes (``null`` clears a field). The budget it leaves must still fit
    together: its scope and period with their fields."""

    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    scope: BudgetScope | None = None
    category_id: CategoryId | None = None
    tag: BudgetTag | None = None
    period: BudgetPeriod | None = None
    start: dt.date | None = None
    end: dt.date | None = None
    limit_minor: Limit | None = None
    alert_threshold: Threshold | None = None


class BudgetState(StrEnum):
    OK = "ok"
    NEAR = "near"  # past the warning share of the limit
    OVER = "over"  # the limit is reached
    UPCOMING = "upcoming"  # dated, and not started yet
    ENDED = "ended"  # dated, and its last day has passed


class BudgetStatus(BaseModel):
    """How a budget stands today, over its current window: this week or month, or its dates."""

    start: dt.date
    end: dt.date
    spent_minor: int
    left_minor: int = Field(description="Negative when over the limit")
    pct: int = Field(description="Share of the limit spent, in whole percent rounded down")
    state: BudgetState
    days_left: int = Field(description="Days of the window still to come, today included")
    per_day_left_minor: int = Field(description="What is left, spread over the days left")
    pace_minor: int = Field(description="What spending at an even pace would reach by today")


class BudgetOut(BudgetFields):
    id: str
    currency: str
    alert_threshold: float
    status: BudgetStatus

    @classmethod
    def of(cls, budget: Budget, status: BudgetStatus) -> BudgetOut:
        return cls.model_validate({**budget.model_dump(), "id": str(budget.id), "status": status})
