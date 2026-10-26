from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import ClassVar

from beanie import PydanticObjectId
from pymongo import ASCENDING, IndexModel

from app.core.documents import OwnedDocument


class PushSubscription(OwnedDocument):
    """A browser on one of the user's devices that agreed to receive pushes (ADR-0018). Its
    keys let only that browser read a message; they are never handed out, not even in an
    export. A browser has one subscription: subscribing it again replaces it, whoever it was.
    It lasts while the device that subscribed it stays signed in, under the server's key."""

    MODULE_KEY: ClassVar[str] = "platform"
    PRIVATE_FIELDS: ClassVar[frozenset[str]] = frozenset({"p256dh", "auth"})

    endpoint: str
    p256dh: str
    auth: str
    session_id: PydanticObjectId | None = None  # the signed-in device (AuthSession) it is on
    key_id: str | None = None  # which VAPID public key it was made with (a fingerprint)
    last_sent_at: datetime | None = None

    class Settings:
        name = "push_subscriptions"
        indexes = [  # noqa: RUF012 - Beanie reads this class attribute
            IndexModel([("endpoint", ASCENDING)], unique=True, name="endpoint_unique"),
            IndexModel([("user_id", ASCENDING), ("created_at", ASCENDING)], name="user_oldest"),
            IndexModel([("session_id", ASCENDING)], name="session"),
        ]

    @classmethod
    async def forget_sessions(cls, session_ids: Iterable[PydanticObjectId | None]) -> None:
        """Signed out on those devices: their browsers get no more pushes."""
        ids = [item for item in session_ids if item is not None]
        if ids:
            await cls.get_pymongo_collection().delete_many({"session_id": {"$in": ids}})
