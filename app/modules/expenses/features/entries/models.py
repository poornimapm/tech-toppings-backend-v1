from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.modules.expenses.domain import MODULE, EntryKind, EntrySource, PaymentMode
from app.platform import (
    CLIENT_KEY_INDEX,
    REVISION_INDEXES,
    OwnedDocument,
    Revision,
    RevisionedDocument,
    RevisionedRepository,
)


class Attachment(BaseModel):
    """A link the user keeps with an entry (a bill, a photo in their own drive). Files are
    never uploaded here."""

    url: str
    label: str | None = None


class Entry(RevisionedDocument):
    """Money spent or received (ADR-0004). Every field is a JSON value, as revisions need."""

    MODULE_KEY: ClassVar[str] = MODULE

    kind: EntryKind
    title: str
    amount_minor: int
    currency: str
    quantity: float | None = None  # how much was bought, in ``unit`` (data/units.yaml)
    unit: str | None = None
    category_id: str  # a system category's key or the id of one of the user's own
    date: str  # the calendar day (YYYY-MM-DD) the user means, never shifted by time zones
    time: str | None = None  # their wall-clock time, if they gave one
    merchant: str | None = None
    payment_mode: PaymentMode | None = None
    for_whom: str | None = None
    tags: list[str] = Field(default_factory=list)
    note: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    source: EntrySource = EntrySource.MANUAL
    recurring_id: str | None = None  # the repeating rule that added it, or whose bill it paid

    class Settings:
        name = "expenses"
        indexes = [  # noqa: RUF012 - Beanie reads this class attribute
            CLIENT_KEY_INDEX,
            IndexModel(
                [("user_id", ASCENDING), ("date", DESCENDING), ("created_at", DESCENDING)],
                name="user_date",
            ),
            IndexModel(
                [("user_id", ASCENDING), ("kind", ASCENDING), ("date", DESCENDING)],
                name="user_kind_date",
            ),
            IndexModel(
                [("user_id", ASCENDING), ("category_id", ASCENDING), ("date", DESCENDING)],
                name="user_category_date",
            ),
        ]


class EntryRevision(Revision):
    class Settings:
        name = "expense_revisions"
        indexes = REVISION_INDEXES


class EntryRepository(RevisionedRepository[Entry]):
    model: ClassVar[type[OwnedDocument]] = Entry
    revisions: ClassVar[type[Revision]] = EntryRevision


async def sums(criteria: Mapping[str, Any], *by: str) -> list[dict[str, Any]]:
    """Total ``amount_minor`` and the number of entries matching ``criteria`` (a repository
    ``query()``), for each combination of the ``by`` fields. It lives beside the model, not in
    the entries service, so figures (analytics, budgets) can read entries without importing the
    service that calls them back after each save."""
    cursor = await Entry.get_pymongo_collection().aggregate(
        [
            {"$match": dict(criteria)},
            {
                "$group": {
                    "_id": {name: f"${name}" for name in by},
                    "amount_minor": {"$sum": "$amount_minor"},
                    "count": {"$sum": 1},
                }
            },
        ]
    )
    return [{**row.pop("_id"), **row} async for row in cursor]
