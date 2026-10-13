from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, ClassVar

from pydantic import Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.core.clock import Clock
from app.core.config import Settings
from app.core.documents import OwnedDocument
from app.core.repository import Scope, ScopedRepository


class Activity(OwnedDocument):
    """Something that happened in the user's account, as a ``type`` code + ``params`` the
    client translates (ADR-0009). Deleted after ``DASHBOARD_ACTIVITY_RETENTION_DAYS`` (TTL)."""

    MODULE_KEY: ClassVar[str] = "platform"

    type: str
    params: dict[str, Any] = Field(default_factory=dict)
    purge_at: datetime

    class Settings:
        name = "activity"
        indexes = [  # noqa: RUF012
            IndexModel([("user_id", ASCENDING), ("created_at", DESCENDING)], name="user_recent"),
            IndexModel([("purge_at", ASCENDING)], expireAfterSeconds=0, name="purge_at_ttl"),
        ]


class ActivityLog(ScopedRepository[Activity]):
    model: ClassVar[type[OwnedDocument]] = Activity

    def __init__(self, scope: Scope, clock: Clock, settings: Settings) -> None:
        super().__init__(scope, clock)
        self._retention = timedelta(days=settings.dashboard.activity_retention_days)

    def module_key_for(self, fields: Mapping[str, Any]) -> str:
        return str(fields["module_key"])

    async def record(self, type_: str, *, module: str = "platform", **params: Any) -> None:
        """Add an entry for the scope's owner: ``record("module.pinned", module="habits")``.
        ``module`` is both the owning module and available to the client as a parameter."""
        await self.create(
            {
                "type": type_,
                "params": {"module": module, **params} if module != "platform" else params,
                "module_key": module,
                "purge_at": self.clock.now() + self._retention,
            }
        )
