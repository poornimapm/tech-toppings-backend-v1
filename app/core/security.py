"""Security primitives with no knowledge of users: password hashing and policy, token
hashing, random secrets, and HS256 JWT encoding.

Argon2 hashing is CPU- and memory-heavy, so it runs in a worker thread to keep the event
loop responsive.
"""

from __future__ import annotations

import hashlib
import secrets
import string
from datetime import datetime
from typing import Any

import anyio
import jwt
from argon2 import PasswordHasher as Argon2Hasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.config import AuthSettings

JWT_ALGORITHM = "HS256"


class PasswordHasher:
    def __init__(self, settings: AuthSettings) -> None:
        self._hasher = Argon2Hasher(
            time_cost=settings.argon2_time_cost,
            memory_cost=settings.argon2_memory_kib,
            parallelism=settings.argon2_parallelism,
        )
        # Verified against when the account doesn't exist, so response time doesn't reveal it.
        self._dummy_hash = self._hasher.hash(secrets.token_urlsafe(16))

    async def hash(self, password: str) -> str:
        return await anyio.to_thread.run_sync(self._hasher.hash, password)

    async def verify(self, password_hash: str | None, password: str) -> bool:
        target = password_hash or self._dummy_hash
        try:
            await anyio.to_thread.run_sync(self._hasher.verify, target, password)
        except (VerificationError, InvalidHashError):
            return False
        return password_hash is not None

    def needs_rehash(self, password_hash: str) -> bool:
        return self._hasher.check_needs_rehash(password_hash)


def password_policy_violations(password: str, settings: AuthSettings) -> list[str]:
    """Rule ids the password breaks (empty list = acceptable)."""
    violations: list[str] = []
    if len(password) < settings.password_min_length:
        violations.append("too_short")
    if len(password) > settings.password_max_length:
        violations.append("too_long")
    classes = sum(
        [
            any(c.islower() for c in password),
            any(c.isupper() for c in password),
            any(c.isdigit() for c in password),
            any(not c.isalnum() for c in password),
        ]
    )
    if classes < settings.password_min_classes:
        violations.append("too_simple")
    if password.strip() != password:
        violations.append("surrounding_whitespace")
    return violations


def generate_one_time_password(length: int = 16) -> str:
    """Readable random password for admin resets (no ambiguous characters)."""
    alphabet = "".join(c for c in string.ascii_letters + string.digits if c not in "0OIl1")
    core = "".join(secrets.choice(alphabet) for _ in range(length))
    return f"{core[:4]}-{core[4:8]}-{core[8:12]}-{core[12:]}"


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """Store only digests of bearer secrets (refresh tokens), never the tokens themselves."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def encode_jwt(claims: dict[str, Any], secret: str) -> str:
    return jwt.encode(claims, secret, algorithm=JWT_ALGORITHM)


def decode_jwt(token: str, secret: str, *, audience: str, issuer: str) -> dict[str, Any]:
    """Verify signature, expiry, audience and issuer. Raises ``jwt.InvalidTokenError``."""
    claims: dict[str, Any] = jwt.decode(
        token,
        secret,
        algorithms=[JWT_ALGORITHM],
        audience=audience,
        issuer=issuer,
        options={"require": ["exp", "iat", "sub", "jti"]},
    )
    return claims


def epoch(moment: datetime) -> int:
    return int(moment.timestamp())
