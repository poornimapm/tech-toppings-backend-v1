"""Short-lived access tokens (JWT, HS256). Only the backend issues and checks them."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

import jwt
from beanie import PydanticObjectId

from app.core.clock import Clock
from app.core.config import AuthSettings
from app.core.security import decode_jwt, encode_jwt, epoch
from app.platform.auth.errors import TokenExpiredError, TokenInvalidError
from app.platform.users.models import Role, User

ACCESS_TOKEN_TYPE = "access"  # noqa: S105 - a token kind label, not a secret


@dataclass(frozen=True)
class AccessClaims:
    user_id: PydanticObjectId
    role: Role
    token_version: int
    session_id: PydanticObjectId  # the signed-in device that obtained this token


class AccessTokenService:
    def __init__(self, settings: AuthSettings, clock: Clock) -> None:
        self.settings = settings
        self.clock = clock

    def issue(self, user: User, session_id: PydanticObjectId) -> tuple[str, int]:
        """Return (token, lifetime in seconds)."""
        now = self.clock.now()
        ttl = self.settings.access_token_ttl_seconds
        claims = {
            "sub": str(user.id),
            "role": user.role.value,
            "tv": user.token_version,
            "sid": str(session_id),
            "typ": ACCESS_TOKEN_TYPE,
            "jti": uuid.uuid4().hex,
            "iat": epoch(now),
            "exp": epoch(now + timedelta(seconds=ttl)),
            "iss": self.settings.jwt_issuer,
            "aud": self.settings.jwt_audience,
        }
        return encode_jwt(claims, self.settings.jwt_secret.get_secret_value()), ttl

    def verify(self, token: str) -> AccessClaims:
        try:
            claims = decode_jwt(
                token,
                self.settings.jwt_secret.get_secret_value(),
                audience=self.settings.jwt_audience,
                issuer=self.settings.jwt_issuer,
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenExpiredError from exc
        except jwt.InvalidTokenError as exc:
            raise TokenInvalidError from exc
        try:
            if claims.get("typ") != ACCESS_TOKEN_TYPE:
                raise TokenInvalidError
            return AccessClaims(
                user_id=PydanticObjectId(claims["sub"]),
                role=Role(claims["role"]),
                token_version=int(claims["tv"]),
                session_id=PydanticObjectId(claims["sid"]),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise TokenInvalidError from exc
