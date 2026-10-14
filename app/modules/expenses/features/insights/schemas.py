from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.platform import RunStatus


class Period(StrEnum):
    WEEK = "week"  # Monday to Sunday
    MONTH = "month"


class InsightQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period: Period = Period.MONTH
    day: dt.date | None = Field(
        default=None, alias="date", description="Any day of it; default today"
    )


class InsightOut(BaseModel):
    """What the week or month looked like: the facts, and a summary of them in the user's
    language (``message`` from the AI, or ``message_code`` + ``message_params`` worded by the
    client when no AI was used)."""

    period: Period
    start: dt.date
    end: dt.date
    status: RunStatus
    mode: str | None
    reason: str | None
    message: str | None
    message_code: str | None
    message_params: dict[str, Any]
    facts: dict[str, Any]
    cached: bool  # shown again: the facts haven't changed since it was written
