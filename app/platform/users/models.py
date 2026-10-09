from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field
from pymongo import ASCENDING, IndexModel

from app.core.config import Locale
from app.core.documents import TimestampedDocument


class Role(StrEnum):
    ADMIN = "admin"
    USER = "user"


class ThemePreference(StrEnum):
    LIGHT = "light"
    DARK = "dark"
    SYSTEM = "system"


class UiMode(StrEnum):
    STANDARD = "standard"
    SIMPLE = "simple"


class AuthProvider(BaseModel):
    provider: Literal["google"]
    subject: str


class User(TimestampedDocument):
    """An account. It is the identity itself, so it has no user_id/module_key of its own."""

    email: str  # stored lower-cased
    name: str
    password_hash: str | None = None  # None for accounts that only use Google sign-in
    role: Role = Role.USER
    locale: Locale
    theme: ThemePreference = ThemePreference.SYSTEM
    ui_mode: UiMode = UiMode.STANDARD
    base_currency: str
    timezone: str
    ai_consent_at: datetime | None = None
    # Bumped on password change, logout-all and admin reset: invalidates every access token.
    token_version: int = 0
    must_change_password: bool = False
    failed_logins: int = 0
    locked_until: datetime | None = None
    auth_providers: list[AuthProvider] = Field(default_factory=list)
    last_login_at: datetime | None = None
    disabled_at: datetime | None = None
    deleted_at: datetime | None = None

    class Settings:
        name = "users"
        indexes = [  # noqa: RUF012 - Beanie reads this class attribute
            IndexModel([("email", ASCENDING)], unique=True, name="email_unique"),
            IndexModel(
                [("auth_providers.provider", ASCENDING), ("auth_providers.subject", ASCENDING)],
                name="auth_provider_subject",
            ),
        ]

    @property
    def is_active(self) -> bool:
        return self.disabled_at is None and self.deleted_at is None
