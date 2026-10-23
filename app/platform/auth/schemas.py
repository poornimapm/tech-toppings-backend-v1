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


class ReauthIn(BaseModel):
    """Proof it's really the account holder: the password, or for an account without one a
    Google sign-in made moments ago (its ID token)."""

    model_config = ConfigDict(extra="forbid")

    password: str | None = Field(default=None, max_length=1024)
    google_id_token: str | None = Field(default=None, max_length=8192)


class PasswordChangeIn(BaseModel):
    """An account without a password (Google only) sets its first one with a fresh Google
    sign-in instead of the current password, plus a code when two-step sign-in is on."""

    model_config = ConfigDict(extra="forbid")

    current_password: str | None = Field(default=None, max_length=1024)
    new_password: str = _PASSWORD_FIELD
    google_id_token: str | None = Field(default=None, max_length=8192)
    code: str | None = Field(default=None, max_length=32)


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


class TwoFactorChallengeOut(BaseModel):
    """Sign-in needs a code too: send it with this token to ``/v1/auth/login/totp``."""

    two_factor_required: Literal[True] = True
    challenge_token: str
    expires_in: int


# ---------------------------------------------------------------------- two-step sign-in

# A six-digit TOTP code or a recovery code (``abcde-fghjk``), spaces allowed.
_CODE_FIELD = Field(min_length=6, max_length=32)


class TwoFactorLoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_token: str = Field(min_length=20, max_length=4096)
    code: str = _CODE_FIELD


class TwoFactorCodeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = _CODE_FIELD


class TwoFactorConfirmIn(ReauthIn):
    """Turning it on or off: a code, and the password (or a fresh Google sign-in)."""

    code: str = _CODE_FIELD


class TwoFactorOut(BaseModel):
    available: bool  # the server can keep secrets (AUTH_FIELD_ENCRYPTION_KEY is set)
    enabled: bool
    enabled_at: datetime | None
    recovery_codes_left: int


class TwoFactorSetupOut(BaseModel):
    secret: str  # base32, for typing into an authenticator app
    otpauth_uri: str  # the same, as a QR code


class RecoveryCodesOut(BaseModel):
    """Shown this once: each works one time in place of a code."""

    recovery_codes: list[str]


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
