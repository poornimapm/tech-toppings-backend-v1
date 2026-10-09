"""Identity lookups. Users are not owned records, so this is not a ScopedRepository; only
platform auth/profile code uses it, always with an id or email it has already verified."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from beanie import PydanticObjectId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.core.clock import Clock
from app.core.config import AuthSettings
from app.core.errors import ConflictError
from app.platform.users.models import User


class EmailTakenError(ConflictError):
    code = "AUTH_EMAIL_TAKEN"
    default_message = "An account with this email already exists."


def normalise_email(email: str) -> str:
    return email.strip().lower()


class UserRepository:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock

    async def by_id(self, user_id: PydanticObjectId) -> User | None:
        return await User.get(user_id)

    async def by_email(self, email: str) -> User | None:
        return await User.find_one({"email": normalise_email(email)})

    async def by_provider(self, provider: str, subject: str) -> User | None:
        return await User.find_one(
            {"auth_providers": {"$elemMatch": {"provider": provider, "subject": subject}}}
        )

    async def create(self, user: User) -> User:
        now = self.clock.now()
        user.email = normalise_email(user.email)
        user.created_at = user.updated_at = now
        try:
            await user.insert()
        except DuplicateKeyError as exc:
            raise EmailTakenError from exc
        return user

    async def update(
        self,
        user_id: PydanticObjectId,
        set_fields: Mapping[str, Any],
        *,
        increment: Mapping[str, int] | None = None,
        push: Mapping[str, Any] | None = None,
    ) -> User:
        """Atomic partial update; returns the updated user."""
        update: dict[str, Any] = {"$set": {**set_fields, "updated_at": self.clock.now()}}
        if increment:
            update["$inc"] = dict(increment)
        if push:
            update["$push"] = dict(push)
        raw = await User.get_pymongo_collection().find_one_and_update(
            {"_id": user_id}, update, return_document=ReturnDocument.AFTER
        )
        if raw is None:
            raise LookupError(f"user {user_id} not found")
        updated: User = User.model_validate(raw)
        return updated

    async def record_failed_login(self, user_id: PydanticObjectId, settings: AuthSettings) -> None:
        """Count a failure; at the threshold and beyond, lock with exponential backoff."""
        now = self.clock.now()
        collection = User.get_pymongo_collection()
        raw = await collection.find_one_and_update(
            {"_id": user_id},
            {"$inc": {"failed_logins": 1}, "$set": {"updated_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        if raw is None:
            return
        failures = int(raw["failed_logins"])
        if failures < settings.lockout_threshold:
            return
        exponent = failures - settings.lockout_threshold
        seconds = min(settings.lockout_base_seconds * 2**exponent, settings.lockout_max_seconds)
        await collection.update_one(
            {"_id": user_id}, {"$set": {"locked_until": now + timedelta(seconds=seconds)}}
        )

    async def record_successful_login(
        self, user_id: PydanticObjectId, *, new_password_hash: str | None = None
    ) -> User:
        fields: dict[str, Any] = {
            "failed_logins": 0,
            "locked_until": None,
            "last_login_at": self.clock.now(),
        }
        if new_password_hash is not None:
            fields["password_hash"] = new_password_hash
        return await self.update(user_id, fields)
