from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from pydantic import Field
from pymongo import ASCENDING, IndexModel

from app.modules.expenses.domain import MODULE
from app.platform import OwnedDocument, ScopedRepository


class Insight(OwnedDocument):
    """An AI-written summary of one week or month, kept with the facts it was written from: while
    the facts are unchanged (same ``facts_hash``) it is shown again without asking the AI."""

    MODULE_KEY: ClassVar[str] = MODULE

    period: str  # week | month
    period_start: str  # its first day, YYYY-MM-DD
    locale: str
    facts_hash: str
    facts: dict[str, Any] = Field(default_factory=dict)
    message: str
    provider: str | None = None
    generated_at: datetime

    class Settings:
        name = "expense_insights"
        indexes = [  # noqa: RUF012 - Beanie reads this class attribute
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("period", ASCENDING),
                    ("period_start", ASCENDING),
                    ("locale", ASCENDING),
                ],
                unique=True,
                name="user_period_locale_unique",
            )
        ]


class InsightRepository(ScopedRepository[Insight]):
    model: ClassVar[type[OwnedDocument]] = Insight
