"""Operator tasks run from scripts/ (no HTTP): bootstrap the first admin, reset a password.

There is no email in v1 (ADR-0011), so a forgotten password is reset by an admin: a one-time
password is generated, shown once, and the user must replace it at the next sign-in.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum

from app.core.clock import Clock
from app.core.config import Settings
from app.core.db import Database
from app.core.logging import get_logger
from app.core.security import (
    PasswordHasher,
    generate_one_time_password,
    password_policy_violations,
)
from app.platform import PLATFORM_DOCUMENTS
from app.platform.auth.sessions import SessionService
from app.platform.users.models import Role, User
from app.platform.users.repository import UserRepository, normalise_email

logger = get_logger(__name__)


class BootstrapOutcome(StrEnum):
    SKIPPED = "skipped"  # BOOTSTRAP_ADMIN_* not configured
    CREATED = "created"
    PROMOTED = "promoted"
    UNCHANGED = "unchanged"


@asynccontextmanager
async def platform_database(settings: Settings) -> AsyncIterator[Database]:
    database = Database(settings.mongo, PLATFORM_DOCUMENTS)
    try:
        if not await database.check_ready():
            raise RuntimeError("MongoDB is not reachable; check MONGO_URI.")
        yield database
    finally:
        await database.close()


async def ensure_bootstrap_admin(
    settings: Settings, clock: Clock, hasher: PasswordHasher
) -> BootstrapOutcome:
    """Create the configured admin, or promote an existing account. Never resets a password."""
    config = settings.bootstrap
    if config.email is None or config.password is None:
        return BootstrapOutcome.SKIPPED
    users = UserRepository(clock)
    existing = await users.by_email(str(config.email))
    if existing is None:
        password = config.password.get_secret_value()
        violations = password_policy_violations(password, settings.auth)
        if violations:
            raise ValueError(f"BOOTSTRAP_ADMIN_PASSWORD does not meet the policy: {violations}")
        now = clock.now()
        await users.create(
            User(
                email=normalise_email(str(config.email)),
                name=config.name,
                password_hash=await hasher.hash(password),
                role=Role.ADMIN,
                locale=settings.defaults.locale,
                base_currency=settings.defaults.currency,
                timezone=settings.defaults.timezone,
                created_at=now,
                updated_at=now,
            )
        )
        logger.info("bootstrap_admin_created")
        return BootstrapOutcome.CREATED
    if existing.role is not Role.ADMIN and existing.id is not None:
        await users.update(existing.id, {"role": Role.ADMIN.value})
        logger.info("bootstrap_admin_promoted", user_id=str(existing.id))
        return BootstrapOutcome.PROMOTED
    return BootstrapOutcome.UNCHANGED


async def reset_password(
    email: str, settings: Settings, clock: Clock, hasher: PasswordHasher
) -> str:
    """Set a one-time password, force a change at next sign-in, sign out every device.
    Returns the one-time password (shown once to the operator, never logged)."""
    users = UserRepository(clock)
    user = await users.by_email(email)
    if user is None or user.id is None:
        raise LookupError("No account with that email.")
    one_time = generate_one_time_password()
    await users.update(
        user.id,
        {
            "password_hash": await hasher.hash(one_time),
            "must_change_password": True,
            "failed_logins": 0,
            "locked_until": None,
        },
        increment={"token_version": 1},
    )
    await SessionService(settings.auth, clock).revoke_all(user.id, "admin_password_reset")
    logger.info("auth_password_reset_by_admin", user_id=str(user.id))
    return one_time
