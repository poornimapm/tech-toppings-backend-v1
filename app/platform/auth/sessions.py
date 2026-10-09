"""Refresh sessions with rotation and reuse detection (ADR-0003).

Each refresh swaps the session's token for a new one. A token that was already swapped out
coming back means someone else holds a copy: the session is revoked, unless it was rotated
moments ago (two tabs refreshing at once), which gets a retryable 409 instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from beanie import PydanticObjectId
from pymongo import ReturnDocument

from app.core.clock import Clock
from app.core.config import AuthSettings
from app.core.logging import get_logger
from app.core.repository import Scope
from app.core.security import hash_token, new_token
from app.platform.auth.errors import RefreshInvalidError, RefreshRaceError, RefreshReusedError
from app.platform.auth.models import AuthSession
from app.platform.auth.repository import SessionRepository
from app.platform.users.models import User

logger = get_logger(__name__)

# Predecessor digests kept per session for reuse detection.
PREVIOUS_TOKENS_KEPT = 50
# Ended sessions are kept this long (for reuse detection) before the TTL index drops them.
PURGE_GRACE = timedelta(days=7)


@dataclass(frozen=True)
class ClientInfo:
    user_agent: str | None
    ip: str | None


@dataclass(frozen=True)
class IssuedRefresh:
    session: AuthSession
    refresh_token: str


class SessionService:
    def __init__(self, settings: AuthSettings, clock: Clock) -> None:
        self.settings = settings
        self.clock = clock

    def _sliding_expiry(self, now: datetime, absolute: datetime) -> datetime:
        return min(now + timedelta(seconds=self.settings.refresh_token_ttl_seconds), absolute)

    async def start(self, user: User, client: ClientInfo) -> IssuedRefresh:
        if user.id is None:
            raise ValueError("user must be saved before starting a session")
        now = self.clock.now()
        absolute = now + timedelta(seconds=self.settings.refresh_absolute_ttl_seconds)
        raw = new_token()
        session = await SessionRepository(Scope.for_user(user.id), self.clock).create(
            {
                "token_hash": hash_token(raw),
                "last_used_at": now,
                "expires_at": self._sliding_expiry(now, absolute),
                "absolute_expires_at": absolute,
                "purge_at": absolute + PURGE_GRACE,
                "user_agent": client.user_agent,
                "ip": client.ip,
            }
        )
        return IssuedRefresh(session=session, refresh_token=raw)

    async def rotate(self, refresh_token: str, client: ClientInfo) -> IssuedRefresh:
        now = self.clock.now()
        digest = hash_token(refresh_token)
        session = await AuthSession.find_one({"token_hash": digest})
        if session is None:
            await self._handle_unknown_token(digest, now)
            raise RefreshInvalidError
        expired = min(session.expires_at, session.absolute_expires_at) <= now
        if session.revoked_at is not None or expired:
            raise RefreshInvalidError

        raw = new_token()
        updated = await AuthSession.get_pymongo_collection().find_one_and_update(
            {"_id": session.id, "token_hash": digest, "revoked_at": None},
            {
                "$set": {
                    "token_hash": hash_token(raw),
                    "rotated_at": now,
                    "last_used_at": now,
                    "updated_at": now,
                    "expires_at": self._sliding_expiry(now, session.absolute_expires_at),
                    "user_agent": client.user_agent,
                    "ip": client.ip,
                },
                "$push": {
                    "previous_token_hashes": {"$each": [digest], "$slice": -PREVIOUS_TOKENS_KEPT}
                },
            },
            return_document=ReturnDocument.AFTER,
        )
        if updated is None:  # another request rotated this token a moment ago
            raise RefreshRaceError
        return IssuedRefresh(session=AuthSession.model_validate(updated), refresh_token=raw)

    async def _handle_unknown_token(self, digest: str, now: datetime) -> None:
        stale = await AuthSession.find_one({"previous_token_hashes": digest})
        if stale is None:
            return
        grace = timedelta(seconds=self.settings.refresh_reuse_grace_seconds)
        recently_rotated = stale.rotated_at is not None and now - stale.rotated_at <= grace
        if stale.revoked_at is None and recently_rotated:
            raise RefreshRaceError
        if stale.revoked_at is None:
            await self._revoke({"_id": stale.id}, "reuse_detected")
            logger.warning(
                "auth_refresh_reuse_detected", user_id=str(stale.user_id), session_id=str(stale.id)
            )
        raise RefreshReusedError

    async def find_by_token(self, refresh_token: str) -> AuthSession | None:
        return await AuthSession.find_one({"token_hash": hash_token(refresh_token)})

    async def revoke_token(self, refresh_token: str, reason: str) -> None:
        await self._revoke({"token_hash": hash_token(refresh_token)}, reason)

    async def revoke_all(
        self, user_id: PydanticObjectId, reason: str, *, keep: PydanticObjectId | None = None
    ) -> int:
        criteria: dict[str, object] = {"user_id": user_id}
        if keep is not None:
            criteria["_id"] = {"$ne": keep}
        return await self._revoke(criteria, reason)

    async def _revoke(self, criteria: dict[str, object], reason: str) -> int:
        now = self.clock.now()
        result = await AuthSession.get_pymongo_collection().update_many(
            {**criteria, "revoked_at": None},
            {"$set": {"revoked_at": now, "revoked_reason": reason, "updated_at": now}},
        )
        return int(result.modified_count)
