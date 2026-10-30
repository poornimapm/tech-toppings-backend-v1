"""Warranties: the module's tunables (data/warranty.yaml), a warranty's end from its length,
and where a receipt's warranty stands on a given day."""

from __future__ import annotations

import calendar
from datetime import date
from enum import StrEnum
from functools import cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

DATA = Path(__file__).parent / "data" / "warranty.yaml"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WarrantyRules(_Strict):
    remind_days_before: int = Field(ge=0)
    max_remind_days: int = Field(ge=0)
    max_months: int = Field(ge=1)
    quick_months: tuple[int, ...]


class DashboardRules(_Strict):
    max_items: int = Field(ge=1)


class Rules(_Strict):
    warranty: WarrantyRules
    dashboard: DashboardRules


@cache
def rules() -> Rules:
    return Rules.model_validate(yaml.safe_load(DATA.read_text(encoding="utf-8")))


class WarrantyStatus(StrEnum):
    NONE = "none"  # no warranty recorded
    ACTIVE = "active"
    ENDING = "ending"  # within its reminder time: ending soon
    ENDED = "ended"


def months_after(day: date, months: int) -> date:
    """The same day ``months`` later, or that month's last day (31 Jan + 1 month: 28 Feb)."""
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def status_on(today: date, ends_on: str | None, remind_on: str | None) -> WarrantyStatus:
    """A warranty covers its last day; it is ending from the day its reminder is due."""
    if ends_on is None:
        return WarrantyStatus.NONE
    if ends_on < today.isoformat():
        return WarrantyStatus.ENDED
    if remind_on is not None and remind_on <= today.isoformat():
        return WarrantyStatus.ENDING
    return WarrantyStatus.ACTIVE


def criteria_for(status: WarrantyStatus, today: date) -> dict[str, object]:
    """The database filter for receipts whose warranty stands at ``status`` on ``today``
    (days are stored as YYYY-MM-DD, so they compare as text)."""
    day = today.isoformat()
    match status:
        case WarrantyStatus.NONE:
            return {"warranty_ends_on": None}
        case WarrantyStatus.ENDED:
            return {"warranty_ends_on": {"$lt": day}}
        case WarrantyStatus.ENDING:
            return {"warranty_ends_on": {"$gte": day}, "remind_on": {"$lte": day}}
        case WarrantyStatus.ACTIVE:
            return {"warranty_ends_on": {"$gte": day}, "remind_on": {"$gt": day}}
