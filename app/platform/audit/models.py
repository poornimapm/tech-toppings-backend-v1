from __future__ import annotations

from datetime import datetime
from typing import Any

from beanie import Document, PydanticObjectId
from pydantic import Field
from pymongo import ASCENDING, DESCENDING, IndexModel


class AuditEntry(Document):
    """One change worth answering for later. ``details`` never holds a secret: a new password,
    a code or a token is never written, only that it changed."""

    at: datetime
    action: str  # e.g. "user.role_changed", "two_factor.enabled", "account.deleted"
    actor_id: PydanticObjectId | None  # who did it (None: the system)
    entity: str  # what it was done to: "user", "allowed_email", "module", "account"
    entity_id: str | None = None
    owner_id: PydanticObjectId | None = None  # whose account it concerns, if anyone's
    details: dict[str, Any] = Field(default_factory=dict)
    ip: str | None = None
    request_id: str | None = None
    purge_at: datetime

    class Settings:
        name = "audit_logs"
        indexes = [  # noqa: RUF012 - Beanie reads this class attribute
            IndexModel([("at", DESCENDING)], name="at"),
            IndexModel([("owner_id", ASCENDING), ("at", DESCENDING)], name="owner_at"),
            IndexModel([("actor_id", ASCENDING), ("at", DESCENDING)], name="actor_at"),
            IndexModel([("entity", ASCENDING), ("entity_id", ASCENDING)], name="entity"),
            IndexModel([("purge_at", ASCENDING)], expireAfterSeconds=0, name="purge_at_ttl"),
        ]
