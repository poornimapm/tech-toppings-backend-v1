from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from pydantic import Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.core.documents import OwnedDocument


class AuthSession(OwnedDocument):
    """One signed-in device. Holds the digest of its current refresh token plus recent
    predecessors: presenting a predecessor again means the token was stolen (or two tabs
    raced), and the whole session is revoked (ADR-0003)."""

    MODULE_KEY: ClassVar[str] = "platform"

    token_hash: str
    previous_token_hashes: list[str] = Field(default_factory=list)
    last_used_at: datetime
    rotated_at: datetime | None = None
    expires_at: datetime  # sliding: pushed forward on each refresh
    absolute_expires_at: datetime  # hard limit for the session
    purge_at: datetime  # TTL index removes the record after this
    revoked_at: datetime | None = None
    revoked_reason: str | None = None
    user_agent: str | None = None
    ip: str | None = None

    class Settings:
        name = "auth_sessions"
        indexes = [  # noqa: RUF012 - Beanie reads this class attribute
            IndexModel([("token_hash", ASCENDING)], unique=True, name="token_hash_unique"),
            IndexModel([("previous_token_hashes", ASCENDING)], name="previous_token_hashes"),
            IndexModel(
                [("user_id", ASCENDING), ("revoked_at", ASCENDING), ("last_used_at", DESCENDING)],
                name="user_active_sessions",
            ),
            IndexModel([("purge_at", ASCENDING)], expireAfterSeconds=0, name="purge_ttl"),
        ]
