from __future__ import annotations

from pydantic import BaseModel, Field

from app.modules.expenses.features.analytics.schemas import (
    CategoryShare,
    DayAmount,
    MerchantShare,
    PersonShare,
    SummaryOut,
)
from app.modules.expenses.features.entries.schemas import EntryOut

MONTH = r"^\d{4}-(0[1-9]|1[0-2])$"


class ReportCategory(CategoryShare):
    previous_minor: int = Field(description="Spent under it in the month before, same days")
    change_pct: float | None = Field(description="None when nothing was spent then")


class MonthlyReportOut(BaseModel):
    """A month on one page: what came in and went out against the month before, where it went,
    and the biggest expenses. A month under way counts up to today, against the same days."""

    month: str = Field(pattern=MONTH)
    summary: SummaryOut
    categories: list[ReportCategory] = Field(description="Money spent, by category")
    income: list[CategoryShare] = Field(description="Money received, by category")
    merchants: list[MerchantShare]
    people: list[PersonShare]
    days: list[DayAmount] = Field(description="Money spent each day")
    largest: list[EntryOut] = Field(description="The biggest expenses, biggest first")
