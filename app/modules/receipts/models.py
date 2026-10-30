"""What the module keeps: receipts, each a thing bought (the record's ``text``) with where,
when and for how much, and when its warranty ends; owned by one user."""

from __future__ import annotations

from typing import ClassVar

from pymongo import ASCENDING, DESCENDING, IndexModel

from app.platform import OwnedDocument, ScopedRepository


class ReceiptsItem(OwnedDocument):
    MODULE_KEY: ClassVar[str] = "receipts"

    text: str  # what was bought
    shop: str | None = None
    bought_on: str  # YYYY-MM-DD
    amount_minor: int | None = None
    currency: str
    bill_number: str | None = None
    serial_number: str | None = None
    warranty_ends_on: str | None = None  # YYYY-MM-DD, its last day covered
    remind_days_before: int = 0
    remind_on: str | None = None  # YYYY-MM-DD: warranty_ends_on less remind_days_before
    note: str | None = None

    class Settings:
        name = "receipts_items"
        indexes = [  # noqa: RUF012 - Beanie reads this class attribute
            IndexModel([("user_id", ASCENDING), ("bought_on", DESCENDING)]),
            IndexModel([("user_id", ASCENDING), ("warranty_ends_on", ASCENDING)]),
        ]


class ReceiptsItemRepository(ScopedRepository[ReceiptsItem]):
    model: ClassVar[type[OwnedDocument]] = ReceiptsItem
