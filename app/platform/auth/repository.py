"""User-facing session management (list/revoke own devices) through the scoped repository."""

from __future__ import annotations

from typing import ClassVar

from beanie import PydanticObjectId

from app.core.documents import OwnedDocument
from app.core.repository import ScopedRepository
from app.platform.auth.models import AuthSession


class SessionRepository(ScopedRepository[AuthSession]):
    model: ClassVar[type[OwnedDocument]] = AuthSession

    async def revoke(self, session_id: str | PydanticObjectId) -> None:
        session = await self.get(session_id)  # 404 for other users' sessions
        if session.revoked_at is None:
            await self.update(
                session_id, {"revoked_at": self.clock.now(), "revoked_reason": "user_revoked"}
            )
