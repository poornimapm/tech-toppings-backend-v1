"""Proof that it is really the account holder, right now, before a change that could lock them
out or erase them: turning two-step sign-in on or off, deleting the account, or giving an account
that has none its first password. The proof is the password when the account has one, else a
Google sign-in made moments ago (``AUTH_GOOGLE_REAUTH_MAX_AGE_SECONDS``). A signed-in session
alone is never enough: it may be stolen."""

from __future__ import annotations

from datetime import timedelta

from app.core.clock import Clock
from app.core.config import AuthSettings
from app.core.errors import AppError
from app.core.security import PasswordHasher
from app.platform.auth.errors import CurrentPasswordInvalidError, GoogleReauthError
from app.platform.auth.google import GoogleIdTokenVerifier
from app.platform.users.models import User


class Reauth:
    def __init__(
        self,
        settings: AuthSettings,
        clock: Clock,
        hasher: PasswordHasher,
        google: GoogleIdTokenVerifier | None,
    ) -> None:
        self.settings = settings
        self.clock = clock
        self.hasher = hasher
        self.google = google

    async def confirm(
        self, user: User, *, password: str | None, google_id_token: str | None
    ) -> None:
        if user.password_hash is not None:
            if not await self.hasher.verify(user.password_hash, password or ""):
                raise CurrentPasswordInvalidError
            return
        await self._google_again(user, google_id_token)

    async def _google_again(self, user: User, id_token: str | None) -> None:
        if not id_token or self.google is None:
            raise GoogleReauthError
        try:
            identity = await self.google.verify(id_token)
        except AppError as exc:
            raise GoogleReauthError from exc
        if identity.subject not in {provider.subject for provider in user.auth_providers}:
            raise GoogleReauthError
        age = self.clock.now() - identity.issued_at
        if age > timedelta(seconds=self.settings.google_reauth_max_age_seconds):
            raise GoogleReauthError
