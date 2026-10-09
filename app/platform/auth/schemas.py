from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints

from app.core.config import RegistrationMode
from app.platform.users.schemas import UserOut

# Upper bound before hashing: keeps a huge body from tying up an Argon2 worker.
_PASSWORD_FIELD = Field(min_length=1, max_length=1024)


class RegisterIn(BaseModel):
    # Only the name is trimmed: passwords are taken exactly as typed (login doesn't trim either).
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    password: str = _PASSWORD_FIELD


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = _PASSWORD_FIELD


class PasswordChangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = _PASSWORD_FIELD
    new_password: str = _PASSWORD_FIELD


class GoogleSignInIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id_token: str = Field(min_length=20, max_length=8192)


class TokenOut(BaseModel):
    """Access token for the Authorization header. The refresh token is never in a body:
    it travels only in the httpOnly cookie."""

    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - OAuth token type, not a secret
    expires_in: int
    user: UserOut


class PasswordPolicyOut(BaseModel):
    min_length: int
    max_length: int
    min_classes: int


class AuthConfigOut(BaseModel):
    registration_mode: RegistrationMode
    google_client_id: str | None
    password_policy: PasswordPolicyOut
    csrf_header: str


class SessionOut(BaseModel):
    id: str
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime
    user_agent: str | None
    ip: str | None
    current: bool
