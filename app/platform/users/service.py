from __future__ import annotations

from typing import Any

from app.core.clock import Clock
from app.platform.users.models import User
from app.platform.users.repository import UserRepository
from app.platform.users.schemas import UserUpdate


class UserService:
    def __init__(self, users: UserRepository, clock: Clock) -> None:
        self.users = users
        self.clock = clock

    async def update_profile(self, user: User, changes: UserUpdate) -> User:
        provided = changes.model_dump(exclude_unset=True)
        updates: dict[str, Any] = {
            key: value
            for key, value in provided.items()
            if key != "ai_consent" and value is not None
        }
        if "ai_consent" in provided and provided["ai_consent"] is not None:
            # Keep the original consent time when consent is re-confirmed.
            if provided["ai_consent"]:
                updates["ai_consent_at"] = user.ai_consent_at or self.clock.now()
            else:
                updates["ai_consent_at"] = None
        if not updates or user.id is None:
            return user
        return await self.users.update(user.id, updates)
