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


class GoalFunding(StrEnum):
    MANUAL = "manual"  # the user adds money by hand (and may take some out)
    NET_SAVINGS = "net_savings"  # grows with what they keep: money in less money out


class Contribution(LedgerItem):
    amount_minor: int  # negative when money was taken out
    date: str  # YYYY-MM-DD, the day the user means
    note: str | None = None


class Goal(RevisionedDocument):
    """Something to save for (U1). A manual goal keeps its contributions and their total; a
    net-savings goal's saved amount is worked out from the entries since ``since``, as the
    analytics do, so it always agrees with them."""

    MODULE_KEY: ClassVar[str] = MODULE

    name: str
    target_minor: int
    currency: str
    target_date: str | None = None  # YYYY-MM-DD
    funding: GoalFunding
    since: str  # YYYY-MM-DD: where net savings start counting
    contributions: list[Contribution] = Field(default_factory=list)
    saved_minor: int = 0  # the contributions' total, kept with them
    # The target last told reached. Kept here because the notification itself is purged after
    # its retention, and a goal can stay reached far longer.
    told_reached_minor: int | None = None

    class Settings:
        name = "expense_goals"
        indexes = [  # noqa: RUF012 - Beanie reads this class attribute
            CLIENT_KEY_INDEX,
            IndexModel([("user_id", ASCENDING), ("created_at", DESCENDING)], name="user_recent"),
        ]


class GoalRevision(Revision):
    class Settings:
        name = "expense_goal_revisions"
        indexes = REVISION_INDEXES


class GoalRepository(RevisionedRepository[Goal]):
    model: ClassVar[type[OwnedDocument]] = Goal
    revisions: ClassVar[type[Revision]] = GoalRevision
    ledgers: ClassVar[frozenset[str]] = frozenset({"contributions"})
