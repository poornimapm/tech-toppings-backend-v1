from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from pydantic import Field
from pymongo import ASCENDING, IndexModel

from app.modules.expenses.domain import MODULE
from app.platform import OwnedDocument, ScopedRepository


class BudgetScope(StrEnum):
    """What a budget counts: all spending, one category, or one tag (a trip, an event)."""

    OVERALL = "overall"
    CATEGORY = "category"
    TAG = "tag"


class BudgetPeriod(StrEnum):
    WEEK = "week"  # Monday to Sunday, again each week
    MONTH = "month"  # again each month
    CUSTOM = "custom"  # once, from start to end


class Budget(OwnedDocument):
    """A limit on spending for a period (U7). What was spent is never stored: it is worked out
    from the entries with the list's own filters, so a budget always agrees with its entries."""

    MODULE_KEY: ClassVar[str] = MODULE

    name: str
    scope: BudgetScope
    category_id: str | None = None
    tag: str | None = None  # casefolded, as entries keep their tags
    period: BudgetPeriod
    start: str | None = None  # YYYY-MM-DD, custom budgets only
    end: str | None = None
    limit_minor: int
    currency: str
    alert_threshold: float  # warn once at this share of the limit; 0 = only when over
    # The last warning told at each level, as "<window start>:<limit>". Kept here because the
    # notification itself is purged after its retention, and a budget can outlast that.
    alerted: dict[str, str] = Field(default_factory=dict)

    class Settings:
        name = "expense_budgets"
        indexes = [  # noqa: RUF012 - Beanie reads this class attribute
            IndexModel(
                [("user_id", ASCENDING), ("scope", ASCENDING), ("period", ASCENDING)],
                name="user_scope_period",
            )
        ]


class BudgetRepository(ScopedRepository[Budget]):
    model: ClassVar[type[OwnedDocument]] = Budget
