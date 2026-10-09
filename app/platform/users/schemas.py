from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.config import Locale
from app.platform.users.models import Role, ThemePreference, UiMode, User


class UserOut(BaseModel):
    id: str
    email: str
    name: str
    role: Role
    locale: Locale
    theme: ThemePreference
    ui_mode: UiMode
    base_currency: str
    timezone: str
    ai_consent: bool
    must_change_password: bool
    has_password: bool
    auth_providers: list[str]
    created_at: datetime

    @classmethod
    def from_user(cls, user: User) -> UserOut:
        return cls(
            id=str(user.id),
            email=user.email,
            name=user.name,
            role=user.role,
            locale=user.locale,
            theme=user.theme,
            ui_mode=user.ui_mode,
            base_currency=user.base_currency,
            timezone=user.timezone,
            ai_consent=user.ai_consent_at is not None,
            must_change_password=user.must_change_password,
            has_password=user.password_hash is not None,
            auth_providers=[provider.provider for provider in user.auth_providers],
            created_at=user.created_at,
        )


class UserUpdate(BaseModel):
    """Fields a user may change about themselves. Anything else is rejected."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=80)
    locale: Locale | None = None
    theme: ThemePreference | None = None
    ui_mode: UiMode | None = None
    base_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    timezone: str | None = Field(default=None, max_length=64)
    ai_consent: bool | None = None

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("unknown time zone") from exc
        return value
