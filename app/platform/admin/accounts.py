"""Managing accounts as an admin: role, disable, sign out everywhere, reset a password. Each
change is audited. An admin never changes their own account here, so the one acting always
stays an admin; and a change that would leave no active admin (two admins demoting each other
at once) is undone (``poe seed`` promotes BOOTSTRAP_ADMIN_EMAIL back, should none be left)."""

from __future__ import annotations

import re
from typing import Any, ClassVar

from beanie import PydanticObjectId

from app.core.errors import ConflictError, NotFoundError
from app.core.pagination import PageParams
from app.core.security import generate_one_time_password
from app.platform.admin.deps import AdminAction
from app.platform.auth.models import AuthSession
from app.platform.auth.sessions import SessionService
from app.platform.users.models import TWO_FACTOR_OFF, Role, User
from app.platform.users.repository import UserRepository


class SelfChangeError(ConflictError):
    code: ClassVar[str] = "ADMIN_SELF_CHANGE"
    default_message = "Change your own account from your account page, not here."


class LastAdminError(ConflictError):
    code: ClassVar[str] = "ADMIN_LAST_ADMIN"
    default_message = "At least one active admin must remain."


ACTIVE_ADMINS: dict[str, Any] = {"role": Role.ADMIN, "disabled_at": None, "deleted_at": None}


STATES: dict[str, dict[str, Any]] = {
    "active": {"disabled_at": None},
    "disabled": {"disabled_at": {"$ne": None}},
    "all": {},
}


class Accounts:
    def __init__(self, action: AdminAction) -> None:
        self.action = action
        self.ctx = action.ctx
        self.users = UserRepository(self.ctx.clock)
        self.sessions = SessionService(self.ctx.settings.auth, self.ctx.clock)

    # ------------------------------------------------------------------ reads

    async def list(
        self, params: PageParams, words: str | None, role: Role | None, state: str
    ) -> tuple[list[User], int]:
        criteria: dict[str, Any] = {"deleted_at": None, **STATES[state]}
        if role is not None:
            criteria["role"] = role
        if words:
            pattern = {"$regex": re.escape(words.strip()), "$options": "i"}
            criteria["$or"] = [{"email": pattern}, {"name": pattern}]
        total = await User.find(criteria).count()
        found = (
            await User.find(criteria)
            .sort("-created_at", "-_id")
            .skip(params.skip)
            .limit(params.page_size)
            .to_list()
        )
        return found, total

    async def one(self, user_id: PydanticObjectId) -> User:
        user = await self.users.by_id(user_id)
        if user is None or user.deleted_at is not None:
            raise NotFoundError
        return user

    async def live_sessions(self, user_id: PydanticObjectId) -> int:
        now = self.ctx.clock.now()
        return await AuthSession.find(
            {"user_id": user_id, "revoked_at": None, "expires_at": {"$gt": now}}
        ).count()

    # ------------------------------------------------------------------ changes

    async def change(
        self, user_id: PydanticObjectId, role: Role | None, disabled: bool | None
    ) -> User:
        user = await self._other(user_id)
        changes: dict[str, Any] = {}
        events: list[tuple[str, dict[str, Any]]] = []
        if role is not None and role is not user.role:
            changes["role"] = role
            events.append(("user.role_changed", {"from": user.role, "to": role}))
        if disabled is not None and disabled != (user.disabled_at is not None):
            changes["disabled_at"] = self.ctx.clock.now() if disabled else None
            events.append(("user.disabled" if disabled else "user.enabled", {}))
        if not changes:
            return user
        # A disabled account, or one whose role moved, signs in again everywhere.
        updated = await self.users.update(user_id, changes, increment={"token_version": 1})
        if user.role is Role.ADMIN and not await User.find(ACTIVE_ADMINS).count():
            # Two admins demoting or disabling each other at once: neither change may stand.
            await self.users.update(user_id, {"role": user.role, "disabled_at": user.disabled_at})
            raise LastAdminError
        if changes.get("disabled_at"):
            await self.sessions.revoke_all(user_id, "admin_disabled")
        for action, details in events:
            await self._record(action, user_id, details)
        return updated

    async def revoke_sessions(self, user_id: PydanticObjectId) -> int:
        await self._other(user_id)
        count = await self.sessions.revoke_all(user_id, "admin_revoked")
        await self.users.update(user_id, {}, increment={"token_version": 1})
        await self._record("user.sessions_revoked", user_id, {"sessions": count})
        return count

    async def reset_password(self, user_id: PydanticObjectId) -> str:
        """A one-time password, returned once: the user must change it at their next sign-in.
        2FA is cleared (the reset is how someone who lost their device gets back in)."""
        user = await self._other(user_id)
        password = generate_one_time_password()
        hashed = await self.ctx.state.password_hasher.hash(password)
        await self.users.update(
            user_id,
            {
                "password_hash": hashed,
                "must_change_password": True,
                "failed_logins": 0,
                "locked_until": None,
                **TWO_FACTOR_OFF,
            },
            increment={"token_version": 1},
        )
        await self.sessions.revoke_all(user_id, "admin_reset")
        await self._record("user.password_reset", user_id, {"two_factor_cleared": user.two_factor})
        return password

    # ------------------------------------------------------------------ helpers

    async def _other(self, user_id: PydanticObjectId) -> User:
        if user_id == self.ctx.user.id:
            raise SelfChangeError
        return await self.one(user_id)

    async def _record(
        self, action: str, user_id: PydanticObjectId, details: dict[str, Any]
    ) -> None:
        await self.action.record(
            action, entity="user", entity_id=str(user_id), owner_id=user_id, details=details
        )
