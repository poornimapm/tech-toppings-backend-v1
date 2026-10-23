"""Two-step sign-in: TOTP codes (RFC 6238) from an authenticator app, with one-use recovery codes.

Turning it on takes two steps: ``setup`` keeps a new secret (encrypted) until ``enable`` sees a
code from it, so a bad scan never locks anyone out. Once on, a sign-in asks for a code after the
password or Google. Each code is accepted once only, and only while the account isn't locked:
the step a TOTP code belongs to is remembered and a recovery code is removed as it is used, each
in one conditional write, so two requests can't spend the same one and guesses sent all at once
can't outrun the lock. Wrong codes count toward the same lockout as wrong passwords.

Turning it on or off also needs the password (or a fresh Google sign-in): the routes ask
``Reauth`` first, so a stolen session alone can't lock the owner out."""

from __future__ import annotations

import math
import re
from typing import Any, Literal, NoReturn

from beanie import PydanticObjectId
from pymongo import ReturnDocument

from app.core.clock import Clock
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.security import (
    FieldCipher,
    hash_token,
    new_recovery_code,
    new_totp_secret,
    totp_matching_step,
    totp_uri,
)
from app.platform.audit.service import AuditLog
from app.platform.auth.errors import (
    AccountLockedError,
    TwoFactorAlreadyOnError,
    TwoFactorCodeInvalidError,
    TwoFactorNotSetUpError,
    TwoFactorOffError,
    TwoFactorUnavailableError,
)
from app.platform.users.models import TWO_FACTOR_OFF, User
from app.platform.users.repository import UserRepository

logger = get_logger(__name__)

# ASCII digits only: \d would also take Devanagari or Tamil digits, which no app shows.
TOTP_CODE = re.compile(r"[0-9]{6}")
Spent = Literal["totp", "recovery"]


def normalised(code: str) -> str:
    """A code as typed, without the spaces apps show it with (``123 456``)."""
    return re.sub(r"\s", "", code)


def recovery_digest(code: str) -> str:
    """How a recovery code is kept: typed in any case, with or without the dash or spaces."""
    return hash_token(re.sub(r"[^a-z0-9]", "", code.lower()))


def _id(user: User) -> PydanticObjectId:
    if user.id is None:  # pragma: no cover - persisted users always have an id
        raise TwoFactorOffError
    return user.id


class TwoFactor:
    def __init__(self, settings: Settings, clock: Clock) -> None:
        self.auth = settings.auth
        self.clock = clock
        self.users = UserRepository(clock)
        self.audit = AuditLog(clock, settings)

    @property
    def available(self) -> bool:
        return self.auth.field_encryption_key is not None

    # ------------------------------------------------------------------ turning it on and off

    async def setup(self, user: User) -> tuple[str, str]:
        """A new secret and the ``otpauth://`` URI to scan. Setting up again replaces it."""
        secret = new_totp_secret()
        sealed = self._cipher().encrypt(secret)
        if await self._write(user, {"totp_enabled_at": None}, {"totp_secret": sealed}) is None:
            raise TwoFactorAlreadyOnError
        return secret, totp_uri(secret, user.email, self.auth.totp_issuer)

    async def enable(self, user: User, code: str, ip: str | None) -> list[str]:
        """Turn it on once a code from the new secret checks out; the recovery codes are
        returned this once only."""
        if user.two_factor:
            raise TwoFactorAlreadyOnError
        if user.totp_secret is None:
            raise TwoFactorNotSetUpError
        self._ensure_unlocked(user)
        code = normalised(code)
        step = self._step(user, code) if TOTP_CODE.fullmatch(code) else None
        if step is None:
            await self._failed(user)
        codes = self._new_recovery_codes()
        changes = {
            "totp_enabled_at": self.clock.now(),
            "totp_last_step": step,
            "recovery_codes": [recovery_digest(item) for item in codes],
        }
        # Only the secret this code was checked against, and only if not turned on meanwhile.
        expected = {"totp_enabled_at": None, "totp_secret": user.totp_secret}
        if await self._write(user, expected, changes) is None:
            fresh = await self.users.by_id(_id(user))
            if fresh is not None:
                self._ensure_unlocked(fresh)  # locked by guesses sent meanwhile
            raise TwoFactorAlreadyOnError if fresh and fresh.two_factor else TwoFactorNotSetUpError
        await self._record("two_factor.enabled", user, ip)
        return codes

    async def disable(self, user: User, code: str, ip: str | None) -> None:
        """Turn it off, with a code as proof (the route has checked the first factor)."""
        self._ensure_on(user)
        spent = await self.check(user, code, ip)
        await self.users.update(_id(user), TWO_FACTOR_OFF)
        await self._record("two_factor.disabled", user, ip, {"with": spent})

    async def renew_recovery_codes(self, user: User, code: str, ip: str | None) -> list[str]:
        """New recovery codes in place of every old one."""
        self._ensure_on(user)
        await self.check(user, code, ip)
        codes = self._new_recovery_codes()
        await self.users.update(
            _id(user), {"recovery_codes": [recovery_digest(item) for item in codes]}
        )
        await self._record("two_factor.recovery_codes_renewed", user, ip)
        return codes

    # ------------------------------------------------------------------ checking a code

    async def check(self, user: User, code: str, ip: str | None) -> Spent:
        """Spend ``code`` (a TOTP code or a recovery code) or count a failed attempt."""
        self._ensure_unlocked(user)
        spent = await self._spend(user, normalised(code))
        if spent is None:
            await self._failed(user)
        if spent == "recovery":
            fresh = await self.users.by_id(_id(user))
            left = len(fresh.recovery_codes) if fresh else 0
            await self._record("two_factor.recovery_code_used", user, ip, {"left": left})
        return spent

    async def _spend(self, user: User, code: str) -> Spent | None:
        if TOTP_CODE.fullmatch(code):
            step = self._step(user, code)
            if step is None:
                return None
            # Only if this step (or a later one) wasn't accepted meanwhile.
            newer = {"$or": [{"totp_last_step": None}, {"totp_last_step": {"$lt": step}}]}
            spent = await self._write(user, newer, {"totp_last_step": step})
            return "totp" if spent is not None else None
        digest = recovery_digest(code)
        spent = await self._write(user, {"recovery_codes": digest}, pull={"recovery_codes": digest})
        return "recovery" if spent is not None else None

    def _step(self, user: User, code: str) -> int | None:
        """The 30-second step ``code`` belongs to, if it's a code from the user's secret."""
        if user.totp_secret is None:
            return None
        secret = self._cipher().decrypt(user.totp_secret)
        if secret is None:
            logger.warning("two_factor_secret_unreadable", user_id=str(user.id))
            raise TwoFactorUnavailableError
        return totp_matching_step(
            secret,
            code,
            self.clock.now(),
            window=self.auth.totp_window_steps,
            after=-1 if user.totp_last_step is None else user.totp_last_step,
        )

    # ------------------------------------------------------------------ helpers

    async def _write(
        self,
        user: User,
        expected: dict[str, Any],
        changes: dict[str, Any] | None = None,
        *,
        pull: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """One conditional write, made only while ``expected`` holds and the account isn't
        locked; the updated document, or None when nothing matched."""
        now = self.clock.now()
        unlocked = {"$or": [{"locked_until": None}, {"locked_until": {"$lte": now}}]}
        update: dict[str, Any] = {"$set": {**(changes or {}), "updated_at": now}}
        if pull:
            update["$pull"] = pull
        found: dict[str, Any] | None = await User.get_pymongo_collection().find_one_and_update(
            {"_id": _id(user), "$and": [unlocked, expected]},
            update,
            return_document=ReturnDocument.AFTER,
        )
        return found

    def _cipher(self) -> FieldCipher:
        if self.auth.field_encryption_key is None:
            raise TwoFactorUnavailableError
        return FieldCipher(self.auth.field_encryption_key)

    def _new_recovery_codes(self) -> list[str]:
        return [new_recovery_code() for _ in range(self.auth.recovery_codes)]

    def _ensure_on(self, user: User) -> None:
        if not user.two_factor:
            raise TwoFactorOffError

    def _ensure_unlocked(self, user: User) -> None:
        now = self.clock.now()
        if user.locked_until is not None and user.locked_until > now:
            raise AccountLockedError(math.ceil((user.locked_until - now).total_seconds()))

    async def _failed(self, user: User) -> NoReturn:
        await self.users.record_failed_login(_id(user), self.auth)
        logger.info("auth_two_factor_failed", user_id=str(user.id))
        raise TwoFactorCodeInvalidError

    async def _record(
        self, action: str, user: User, ip: str | None, details: dict[str, object] | None = None
    ) -> None:
        await self.audit.record(
            action,
            actor_id=user.id,
            entity="user",
            entity_id=str(user.id),
            owner_id=user.id,
            details=details,
            ip=ip,
        )
