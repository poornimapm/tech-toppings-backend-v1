from __future__ import annotations

import datetime as dt
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.modules.expenses.features.entries.schemas import Name, Note
from app.modules.expenses.features.goals.models import Goal, GoalFunding
from app.modules.expenses.ledger import MAX_MINOR, LedgerAmount

Target = Annotated[int, Field(gt=0, le=MAX_MINOR, description="In the base currency's minor unit")]


class GoalFields(BaseModel):
    name: Name
    target_minor: Target
    target_date: dt.date | None = Field(default=None, description="When it should be reached")
    funding: GoalFunding = GoalFunding.MANUAL
    since: dt.date | None = Field(
        default=None, description="Net-savings goals count from this day (default: today)"
    )


class GoalIn(GoalFields):
    model_config = ConfigDict(extra="forbid")


class GoalPatch(BaseModel):
    """Only what is sent changes (``null`` clears the date), plus the revision that was edited:
    if the goal has moved on since (money was added), the answer is 409."""

    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    target_minor: Target | None = None
    target_date: dt.date | None = None
    funding: GoalFunding | None = None
    since: dt.date | None = None
    revision_no: int = Field(ge=1)


class ContributionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount_minor: Annotated[LedgerAmount, Field(description="Negative to take money out")]
    date: dt.date | None = Field(default=None, description="Default today; never after today")
    note: Note = None


class Progress(BaseModel):
    """How far a goal has come today."""

    saved_minor: int
    left_minor: int = Field(description="Still to save; 0 once reached")
    pct: int = Field(description="Share of the target saved, in whole percent rounded down")
    per_month_minor: int | None = Field(
        description="To save each month, this one included, to reach it by its date; null "
        "without a date, once reached, or when the date has passed"
    )
    reached: bool


class GoalOut(Progress, GoalFields):
    id: str
    currency: str
    since: dt.date
    revision_no: int

    @classmethod
    def of(cls, goal: Goal, progress: Progress) -> GoalOut:
        return cls.model_validate(
            {**goal.model_dump(), **progress.model_dump(), "id": str(goal.id)}
        )
