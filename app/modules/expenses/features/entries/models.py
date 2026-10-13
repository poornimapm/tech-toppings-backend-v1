from __future__ import annotations

from typing import ClassVar

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
