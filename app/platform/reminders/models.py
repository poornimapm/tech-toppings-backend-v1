from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from pydantic import Field
from pymongo import ASCENDING, IndexModel

from app.core.documents import OwnedDocument


class Reminder(OwnedDocument):
    """One module record's due date. ``module_key`` is the scheduling module; ``deleted_at``
    means the module cancelled it. The unique (module, record, due date) keeps every row, sent
    and dismissed ones included, until ``purge_at`` (TTL), so a due date is never told twice."""

    MODULE_KEY: ClassVar[str] = "platform"

    type: str  # a code the client words, e.g. "expenses.bill_due"
    ref_id: str  # the module's record
    params: dict[str, Any] = Field(default_factory=dict)
    due_on: str  # the calendar day (YYYY-MM-DD) it is due
    remind_at: datetime
    channels: list[str] = Field(default_factory=lambda: ["in_app"])
    round: int = 0  # bumped by a snooze: each round is told once
    sent_at: datetime | None = None
    snoozed_until: datetime | None = None
    dismissed_at: datetime | None = None
    purge_at: datetime

    class Settings:
        name = "reminders"
        indexes = [  # noqa: RUF012
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("module_key", ASCENDING),
                    ("ref_id", ASCENDING),
                    ("due_on", ASCENDING),
                ],
                unique=True,
                name="ref_due_unique",
            ),
            IndexModel(
                [("user_id", ASCENDING), ("sent_at", ASCENDING), ("remind_at", ASCENDING)],
                name="user_pending",
            ),
            IndexModel([("user_id", ASCENDING), ("due_on", ASCENDING)], name="user_due"),
            IndexModel([("purge_at", ASCENDING)], expireAfterSeconds=0, name="purge_at_ttl"),
        ]
