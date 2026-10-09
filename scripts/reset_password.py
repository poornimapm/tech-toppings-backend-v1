"""Admin password reset (there is no email in v1, see ADR-0011).

Sets a one-time password, signs the user out of every device and forces a new password at the
next sign-in. The one-time password is printed once here and never logged.
Usage: ``poe reset-password --email someone@example.com``
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from app.core.clock import SystemClock
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.security import PasswordHasher
from app.platform.auth.maintenance import platform_database, reset_password


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--email", required=True, help="Email of the account to reset")
    return parser.parse_args(argv)


async def run(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log)
    async with platform_database(settings):
        try:
            one_time = await reset_password(
                args.email, settings, SystemClock(), PasswordHasher(settings.auth)
            )
        except LookupError as exc:
            print(f"error: {exc}")
            return 1
    print(f"One-time password for {args.email}: {one_time}")
    print("Share it privately. The user must choose a new password at the next sign-in;")
    print("all of their devices have been signed out.")
    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
