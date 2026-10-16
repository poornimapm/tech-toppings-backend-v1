from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from pydantic import Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.modules.expenses.domain import MODULE
from app.modules.expenses.ledger import LedgerItem
from app.platform import (
    CLIENT_KEY_INDEX,
    REVISION_INDEXES,
    OwnedDocument,
    Revision,
    RevisionedDocument,
    RevisionedRepository,
)


class IouDirection(StrEnum):
    LENT = "lent"  # the user gave the money: it is owed to them
    BORROWED = "borrowed"  # the user took it: they owe it


class Settlement(LedgerItem):
    amount_minor: int  # a payment; negative to correct one
    date: str  # YYYY-MM-DD
    note: str | None = None


class Iou(RevisionedDocument):
    """Money lent or borrowed between the user and someone (U2), and the payments on it. It is
    a ledger of its own: it never adds to or changes the user's entries."""

    MODULE_KEY: ClassVar[str] = MODULE

    counterparty: str
    counterparty_key: str  # the name as compared ("Ravi" and "ravi " are one person)
    direction: IouDirection
    amount_minor: int
    currency: str
    date: str  # YYYY-MM-DD, when the money changed hands
    due_on: str | None = None  # YYYY-MM-DD
    note: str | None = None
    settlements: list[Settlement] = Field(default_factory=list)
    settled_minor: int = 0  # the settlements' total, kept with them
    settled_on: str | None = None  # YYYY-MM-DD once paid in full; None while open

    class Settings:
        name = "expense_ious"
        indexes = [  # noqa: RUF012 - Beanie reads this class attribute
            CLIENT_KEY_INDEX,
            IndexModel(
                [("user_id", ASCENDING), ("settled_on", ASCENDING), ("due_on", ASCENDING)],
                name="user_open_due",
            ),
            IndexModel(
                [("user_id", ASCENDING), ("counterparty_key", ASCENDING), ("date", DESCENDING)],
                name="user_counterparty",
            ),
        ]


class IouRevision(Revision):
    class Settings:
        name = "expense_iou_revisions"
        indexes = REVISION_INDEXES


class IouRepository(RevisionedRepository[Iou]):
    model: ClassVar[type[OwnedDocument]] = Iou
    revisions: ClassVar[type[Revision]] = IouRevision
    ledgers: ClassVar[frozenset[str]] = frozenset({"settlements"})
