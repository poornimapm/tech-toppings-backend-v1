from __future__ import annotations

from datetime import datetime

from beanie import Document
from pymongo import ASCENDING, IndexModel


class FxRate(Document):
    """One currency's rates as asked for a day: shared by every user (rates are public)."""

    base: str  # ISO 4217
    day: str  # YYYY-MM-DD as asked
    rate_date: str  # the day the rates are from (the last working day up to ``day``)
    rates: dict[str, str]  # currency -> how much of it one ``base`` buys, as a decimal string
    fetched_at: datetime

    class Settings:
        name = "fx_rates"
        indexes = [  # noqa: RUF012 - Beanie reads this class attribute
            IndexModel([("base", ASCENDING), ("day", ASCENDING)], unique=True, name="base_day")
        ]
