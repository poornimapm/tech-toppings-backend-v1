"""Authentication use cases. Routers stay thin; every rule lives here."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

from app.core.clock import Clock
from app.core.config import RegistrationMode, Settings
from app.core.logging import get_logger
from app.core.security import PasswordHasher, password_policy_violations
from app.platform.auth.errors import (
    AccountDisabledError,
    AccountLockedError,
    CurrentPasswordInvalidError,
    GoogleSignInDisabledError,
    InvalidCredentialsError,
    PasswordPolicyError,
    RefreshInvalidError,
    RegistrationClosedError,
    RegistrationNotAllowedError,
)
from app.platform.auth.google import GoogleIdTokenVerifier
from app.platform.auth.schemas import (
    AuthConfigOut,
    LoginIn,
    PasswordChangeIn,
    PasswordPolicyOut,
    RegisterIn,
)
from app.platform.auth.sessions import ClientInfo, IssuedRefresh, SessionService
from app.platform.auth.tokens import AccessTokenService
from app.platform.users.models import AuthProvider, User
from app.platform.users.repository import EmailTakenError, UserRepository, normalise_email

logger = get_logger(__name__)

GOOGLE: Final = "google"


@dataclass(frozen=True)
class AuthResult:
    user: User
    access_token: str
    expires_in: int
    refresh: IssuedRefresh


class AuthService:
    def __init__(  # noqa: PLR0913 - keyword-only dependencies
        self,
        *,
        settings: Settings,
        clock: Clock,
        users: UserRepository,
        sessions: SessionService,
        tokens: AccessTokenService,
        hasher: PasswordHasher,
        google: GoogleIdTokenVerifier | None,
    ) -> None:
        self.settings = settings
        self.auth = settings.auth
        self.clock = clock
        self.users = users
        self.sessions = sessions
        self.tokens = tokens
        self.hasher = hasher
        self.google = google

    # ------------------------------------------------------------------ queries

    def config(self) -> AuthConfigOut:
        return AuthConfigOut(
            registration_mode=self.auth.registration_mode,
            google_client_id=self.auth.google_client_id,
            password_policy=PasswordPolicyOut(
                min_length=self.auth.password_min_length,
                max_length=self.auth.password_max_length,
                min_classes=self.auth.password_min_classes,
            ),
            csrf_header=self.auth.csrf_header,
        )

    # ------------------------------------------------------------------ commands

    async def register(self, data: RegisterIn, client: ClientInfo) -> AuthResult:
        email = normalise_email(str(data.email))
        self._ensure_can_register(email)
        self._ensure_password_policy(data.password)
        if await self.users.by_email(email) is not None:
            raise EmailTakenError
        user = await self.users.create(
            self._new_user(email, data.name, await self.hasher.hash(data.password))
        )
        logger.info("auth_registered", user_id=str(user.id))
        return await self._issue(user, client)

    async def login(self, data: LoginIn, client: ClientInfo) -> AuthResult:
        user = await self.users.by_email(str(data.email))
        now = self.clock.now()
        if user is not None and user.locked_until is not None and user.locked_until > now:
            raise AccountLockedError(math.ceil((user.locked_until - now).total_seconds()))

        valid = await self.hasher.verify(user.password_hash if user else None, data.password)
        if user is None or user.id is None or not valid:
            if user is not None and user.id is not None:
                await self.users.record_failed_login(user.id, self.auth)
            logger.info("auth_login_failed", reason="invalid_credentials")
            raise InvalidCredentialsError
        if not user.is_active:
            raise AccountDisabledError

        rehash = None
        if user.password_hash and self.hasher.needs_rehash(user.password_hash):
            rehash = await self.hasher.hash(data.password)
        user = await self.users.record_successful_login(user.id, new_password_hash=rehash)
        logger.info("auth_login_succeeded", user_id=str(user.id))
        return await self._issue(user, client)

    async def refresh(self, refresh_token: str | None, client: ClientInfo) -> AuthResult:
        if not refresh_token:
            raise RefreshInvalidError
        issued = await self.sessions.rotate(refresh_token, client)
        user = await self.users.by_id(issued.session.user_id)
        if user is None or not user.is_active:
            await self.sessions.revoke_token(issued.refresh_token, "account_inactive")
            raise RefreshInvalidError
        return self._result(user, issued)

    async def logout(self, refresh_token: str | None) -> None:
        if refresh_token:
            await self.sessions.revoke_token(refresh_token, "logout")

    async def logout_all(self, user: User) -> None:
        if user.id is None:
            return
        await self.sessions.revoke_all(user.id, "logout_all")
        await self.users.update(user.id, {}, increment={"token_version": 1})
        logger.info("auth_logout_all", user_id=str(user.id))

    async def change_password(
        self, user: User, data: PasswordChangeIn, client: ClientInfo
    ) -> AuthResult:
        user_id = user.id
        if user_id is None:
            raise InvalidCredentialsError
        # Accounts created through Google have no password yet and may set one directly.
        if user.password_hash is not None and not await self.hasher.verify(
            user.password_hash, data.current_password
        ):
            raise CurrentPasswordInvalidError
        self._ensure_password_policy(data.new_password)
        if data.new_password == data.current_password:
            raise PasswordPolicyError(details=self._policy_details(["same_as_current"]))
        new_hash = await self.hasher.hash(data.new_password)
        user = await self.users.update(
            user_id,
            {"password_hash": new_hash, "must_change_password": False},
            increment={"token_version": 1},
        )
        await self.sessions.revoke_all(user_id, "password_changed")
        logger.info("auth_password_changed", user_id=str(user_id))
        return await self._issue(user, client)

    async def google_sign_in(self, id_token: str, client: ClientInfo) -> AuthResult:
        if self.google is None:
            raise GoogleSignInDisabledError
        identity = await self.google.verify(id_token)
        user = await self.users.by_provider(GOOGLE, identity.subject)
        if user is None:
            existing = await self.users.by_email(identity.email)
            if existing is not None and existing.id is not None:
                # Google verified the address, so linking to the matching account is safe.
                user = await self.users.update(
                    existing.id,
                    {},
                    push={"auth_providers": {"provider": GOOGLE, "subject": identity.subject}},
                )
            else:
                self._ensure_can_register(identity.email)
                user = await self.users.create(
                    self._new_user(
                        identity.email,
                        identity.name or identity.email.split("@")[0],
                        None,
                        providers=[AuthProvider(provider=GOOGLE, subject=identity.subject)],
                    )
                )
        if not user.is_active or user.id is None:
            raise AccountDisabledError
        user = await self.users.record_successful_login(user.id)
        logger.info("auth_google_sign_in", user_id=str(user.id))
        return await self._issue(user, client)

    # ------------------------------------------------------------------ helpers

    def _is_bootstrap_admin(self, email: str) -> bool:
        admin = self.settings.bootstrap.email
        return admin is not None and normalise_email(str(admin)) == email

    def _ensure_can_register(self, email: str) -> None:
        email = normalise_email(email)
        if self._is_bootstrap_admin(email):
            return
        mode = self.auth.registration_mode
        if mode is RegistrationMode.CLOSED:
            raise RegistrationClosedError
        if mode is RegistrationMode.ALLOWLIST and email not in self.auth.allowed_emails:
            raise RegistrationNotAllowedError

    def _policy_details(self, violations: list[str]) -> dict[str, object]:
        return {
            "violations": violations,
            "min_length": self.auth.password_min_length,
            "min_classes": self.auth.password_min_classes,
        }

    def _ensure_password_policy(self, password: str) -> None:
        violations = password_policy_violations(password, self.auth)
        if violations:
            raise PasswordPolicyError(details=self._policy_details(violations))

    def _new_user(
        self,
        email: str,
        name: str,
        password_hash: str | None,
        *,
        providers: list[AuthProvider] | None = None,
    ) -> User:
        defaults = self.settings.defaults
        now = self.clock.now()
        return User(
            email=email,
            name=name.strip(),
            password_hash=password_hash,
            locale=defaults.locale,
            base_currency=defaults.currency,
            timezone=defaults.timezone,
            auth_providers=providers or [],
            last_login_at=now,
            created_at=now,
            updated_at=now,
        )

    async def _issue(self, user: User, client: ClientInfo) -> AuthResult:
        return self._result(user, await self.sessions.start(user, client))

    def _result(self, user: User, issued: IssuedRefresh) -> AuthResult:
        if issued.session.id is None:  # pragma: no cover - inserted sessions always have an id
            raise RefreshInvalidError
        token, expires_in = self.tokens.issue(user, issued.session.id)
        return AuthResult(user=user, access_token=token, expires_in=expires_in, refresh=issued)
