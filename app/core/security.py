"""Security primitives with no knowledge of users: password hashing and policy, token
hashing, random secrets, shared-secret checks, and HS256 JWT encoding.

Argon2 hashing is CPU- and memory-heavy, so it runs in a worker thread to keep the event
loop responsive.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import string
import struct
from datetime import datetime
from typing import Any
from urllib.parse import quote

import anyio
import jwt
from argon2 import PasswordHasher as Argon2Hasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

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


def same_secret(given: str | None, expected: SecretStr | None) -> bool:
    """Constant-time check of a presented shared secret, so timing reveals nothing of it.
    Nothing presented, or nothing configured, never matches."""
    if not given or expected is None:
        return False
    return hmac.compare_digest(given.encode("utf-8"), expected.get_secret_value().encode("utf-8"))


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


# ---------------------------------------------------------------------- TOTP (RFC 6238)

TOTP_STEP_SECONDS = 30
TOTP_DIGITS = 6
TOTP_SECRET_BYTES = 20  # 160 bits, as RFC 4226 recommends
RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i


def new_totp_secret() -> str:
    """A shared secret, base32 as authenticator apps take it."""
    return base64.b32encode(secrets.token_bytes(TOTP_SECRET_BYTES)).decode().rstrip("=")


def totp_step(moment: datetime) -> int:
    return int(moment.timestamp()) // TOTP_STEP_SECONDS


def totp_code(secret: str, step: int) -> str:
    """The code for one 30-second step (HMAC-SHA1, dynamic truncation, six digits)."""
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{number % 10**TOTP_DIGITS:0{TOTP_DIGITS}d}"


def totp_matching_step(
    secret: str, code: str, moment: datetime, *, window: int, after: int
) -> int | None:
    """The step ``code`` belongs to, within ``window`` steps of now and later than ``after`` (a
    code already used never works twice), else None. Compared in constant time."""
    now = totp_step(moment)
    for step in range(now - window, now + window + 1):
        if step > after and hmac.compare_digest(totp_code(secret, step).encode(), code.encode()):
            return step
    return None


def totp_uri(secret: str, account: str, issuer: str) -> str:
    """What an authenticator app scans (or is typed from)."""
    label = quote(f"{issuer}:{account}")
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}"
        f"&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_STEP_SECONDS}"
    )


def new_recovery_code() -> str:
    """``abcde-fghjk``: ten readable characters, about 50 bits."""
    core = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(10))
    return f"{core[:5]}-{core[5:]}"


class FieldCipher:
    """Encrypts a field at rest (TOTP secrets) with Fernet (AES-128-CBC + HMAC-SHA256)."""

    def __init__(self, key: SecretStr) -> None:
        self._fernet = Fernet(key.get_secret_value().encode())

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str | None:
        """The value, or None when it can't be read (another key, or tampered with)."""
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except InvalidToken:
            return None
