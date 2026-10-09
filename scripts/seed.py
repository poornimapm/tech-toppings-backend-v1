"""Idempotent seed data. Safe to run repeatedly.

Phase 2: creates the bootstrap admin from BOOTSTRAP_ADMIN_EMAIL / BOOTSTRAP_ADMIN_PASSWORD (or
promotes that account if it already exists). Later phases add system data (categories, units).
Usage: ``poe seed``
"""

from __future__ import annotations

import asyncio

from app.core.clock import SystemClock
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.security import PasswordHasher
from app.platform.auth.maintenance import ensure_bootstrap_admin, platform_database


async def run() -> int:
    settings = get_settings()
    configure_logging(settings.log)
    async with platform_database(settings):
        outcome = await ensure_bootstrap_admin(
            settings, SystemClock(), PasswordHasher(settings.auth)
        )
    print(f"bootstrap admin: {outcome.value}")
    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
