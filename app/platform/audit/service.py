from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping
from typing import Any

from beanie import PydanticObjectId

from app.core.clock import Clock
from app.core.config import Settings
from app.core.context import current_request_id
from app.platform.audit.models import AuditEntry

# Keys whose values are never written, whoever passes them: only that they were there.
SECRET_KEYS = re.compile(r"password|secret|token|code|otp|key", re.IGNORECASE)
REDACTED = "[redacted]"


def _clean(value: Any) -> Any:
    if isinstance(value, Mapping):
        return redacted(value)
    if isinstance(value, list | tuple):
        return [_clean(item) for item in value]
    return value


def redacted(details: Mapping[str, Any]) -> dict[str, Any]:
    """``details`` with any secret-looking value replaced, all the way down (lists too)."""
    return {
        name: REDACTED if SECRET_KEYS.search(name) else _clean(value)
        for name, value in details.items()
    }


class AuditLog:
    def __init__(self, clock: Clock, settings: Settings) -> None:
        self.clock = clock
        self.retention = dt.timedelta(days=settings.audit.retention_days)

    async def record(  # noqa: PLR0913 - keyword-only facts of one change
        self,
        action: str,
        *,
        actor_id: PydanticObjectId | None,
        entity: str,
        entity_id: str | None = None,
        owner_id: PydanticObjectId | None = None,
        details: Mapping[str, Any] | None = None,
        ip: str | None = None,
    ) -> AuditEntry:
        now = self.clock.now()
        entry = AuditEntry(
            at=now,
            action=action,
            actor_id=actor_id,
            entity=entity,
            entity_id=entity_id,
            owner_id=owner_id,
            details=redacted(details or {}),
            ip=ip,
            request_id=current_request_id(),
            purge_at=now + self.retention,
        )
        await entry.insert()
        return entry
