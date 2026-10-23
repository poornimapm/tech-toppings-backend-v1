"""Short-lived tokens (JWT, HS256). Only the backend issues and checks them: access tokens, and
the two-factor challenge that stands between a correct password and the code."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import jwt
from beanie import PydanticObjectId

from app.core.clock import Clock
from app.core.config import AuthSettings
from app.core.errors import AppError
from app.core.security import decode_jwt, encode_jwt, epoch
from app.platform.auth.errors import (
    TokenExpiredError,
    TokenInvalidError,
    TwoFactorChallengeInvalidError,
)
from app.platform.users.models import Role, User

ACCESS_TOKEN_TYPE = "access"  # noqa: S105 - a token kind label, not a secret
CHALLENGE_TOKEN_TYPE = "two_factor"  # noqa: S105 - a token kind label, not a secret


@dataclass(frozen=True)
class AccessClaims:
    user_id: PydanticObjectId
    role: Role
    token_version: int
    session_id: PydanticObjectId  # the signed-in device that obtained this token


@dataclass(frozen=True)
class ChallengeClaims:
    user_id: PydanticObjectId
    token_version: int  # a password change or reset since then ends the challenge


class AccessTokenService:
    def __init__(self, settings: AuthSettings, clock: Clock) -> None:
        self.settings = settings
        self.clock = clock

    def issue(self, user: User, session_id: PydanticObjectId) -> tuple[str, int]:
        """Return (token, lifetime in seconds)."""
        ttl = self.settings.access_token_ttl_seconds
        claims = {"role": user.role.value, "sid": str(session_id)}
        return self._encode(user, claims, ACCESS_TOKEN_TYPE, ttl), ttl

    def issue_challenge(self, user: User) -> tuple[str, int]:
        """Proof the first factor passed, exchanged with a code at ``/login/totp``."""
        ttl = self.settings.totp_challenge_seconds
        return self._encode(user, {}, CHALLENGE_TOKEN_TYPE, ttl), ttl

    def verify(self, token: str) -> AccessClaims:
        claims = self._decode(token, ACCESS_TOKEN_TYPE, TokenExpiredError, TokenInvalidError)
        try:
            return AccessClaims(
                user_id=PydanticObjectId(claims["sub"]),
                role=Role(claims["role"]),
                token_version=int(claims["tv"]),
                session_id=PydanticObjectId(claims["sid"]),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise TokenInvalidError from exc

    def verify_challenge(self, token: str) -> ChallengeClaims:
        invalid = TwoFactorChallengeInvalidError
        claims = self._decode(token, CHALLENGE_TOKEN_TYPE, invalid, invalid)
        try:
            return ChallengeClaims(PydanticObjectId(claims["sub"]), int(claims["tv"]))
        except (KeyError, ValueError, TypeError) as exc:
            raise invalid from exc

    def _encode(self, user: User, claims: dict[str, Any], kind: str, ttl: int) -> str:
        now = self.clock.now()
        payload = {
            **claims,
            "sub": str(user.id),
            "tv": user.token_version,
            "typ": kind,
            "jti": uuid.uuid4().hex,
            "iat": epoch(now),
            "exp": epoch(now + timedelta(seconds=ttl)),
            "iss": self.settings.jwt_issuer,
            "aud": self.settings.jwt_audience,
        }
        return encode_jwt(payload, self.settings.jwt_secret.get_secret_value())

    def _decode(
        self, token: str, kind: str, expired: type[AppError], invalid: type[AppError]
    ) -> dict[str, Any]:
        """Verified claims of a token of this kind (an access token is never a challenge)."""
        try:
            claims = decode_jwt(
                token,
                self.settings.jwt_secret.get_secret_value(),
                audience=self.settings.jwt_audience,
                issuer=self.settings.jwt_issuer,
            )
        except jwt.ExpiredSignatureError as exc:
            raise expired from exc
        except jwt.InvalidTokenError as exc:
            raise invalid from exc
        if claims.get("typ") != kind:
            raise invalid
        return claims
