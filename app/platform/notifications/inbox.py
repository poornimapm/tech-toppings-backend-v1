from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any, ClassVar

from beanie import PydanticObjectId
from pydantic import Field
from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.errors import DuplicateKeyError

from app.core.clock import Clock
from app.core.config import Settings
from app.core.documents import OwnedDocument
from app.core.repository import Scope, ScopedRepository


class Notification(OwnedDocument):
    """Kept for ``NOTIFICATIONS_RETENTION_DAYS`` (TTL). ``dedupe_key`` makes a notification
    happen once per user however often its trigger is seen again."""

    MODULE_KEY: ClassVar[str] = "platform"

    type: str
    params: dict[str, Any] = Field(default_factory=dict)
    read_at: datetime | None = None
    dedupe_key: str | None = None
    purge_at: datetime

    class Settings:
        name = "notifications"
        indexes = [  # noqa: RUF012
            IndexModel(
                [("user_id", ASCENDING), ("read_at", ASCENDING), ("created_at", DESCENDING)],
                name="user_unread_recent",
            ),
            IndexModel(
                [("user_id", ASCENDING), ("dedupe_key", ASCENDING)],
                unique=True,
                partialFilterExpression={"dedupe_key": {"$type": "string"}},
                name="user_dedupe_unique",
            ),
            IndexModel([("purge_at", ASCENDING)], expireAfterSeconds=0, name="purge_at_ttl"),
        ]


class Inbox(ScopedRepository[Notification]):
    """One user's notifications. Any platform or module code notifies through this:
    ``notify("module.launched", module="habits")``. As in the activity feed, ``params.module``
    names the module when there is one."""

    model: ClassVar[type[OwnedDocument]] = Notification

    def __init__(self, scope: Scope, clock: Clock, settings: Settings) -> None:
        super().__init__(scope, clock)
        self._retention = timedelta(days=settings.notifications.retention_days)

    def module_key_for(self, fields: Mapping[str, Any]) -> str:
        return str(fields["module_key"])

    async def notify(
        self, type_: str, *, module: str = "platform", dedupe_key: str | None = None, **params: Any
    ) -> None:
        with suppress(DuplicateKeyError):  # already told once: the point of dedupe_key
            await self.create(
                {
                    "type": type_,
                    "params": {"module": module, **params} if module != "platform" else params,
                    "module_key": module,
                    "dedupe_key": dedupe_key,
                    "purge_at": self.clock.now() + self._retention,
                }
            )

    async def mark_read(self, notification_id: str | PydanticObjectId) -> Notification:
        return await self.update(notification_id, {"read_at": self.clock.now()})

    async def mark_all_read(self) -> int:
        now = self.clock.now()
        result = await Notification.get_pymongo_collection().update_many(
            self.query({"read_at": None}),
            {"$set": {"read_at": now, "updated_at": now, "updated_by": self.scope.actor_id}},
        )
        return result.modified_count
