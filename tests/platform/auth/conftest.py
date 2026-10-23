"""Helpers for tests with two-step sign-in on: an account with it on, and its codes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient

from app.core.security import totp_code, totp_step
from tests.helpers import PASSWORD, Account, FrozenClock, register

TOTP = "/v1/auth/totp"


@dataclass
class TwoFactorAccount:
    account: Account
    secret: str
    recovery_codes: list[str]


def earlier_clock(minutes: int = 3) -> FrozenClock:
    """A clock started in the past: tests move it forward for fresh codes, and the tokens it
    dates must not lie in the future (PyJWT checks ``iat`` against the real time)."""
    return FrozenClock(datetime.now(UTC) - timedelta(minutes=minutes))


def code_now(secret: str, clock: FrozenClock) -> str:
    return totp_code(secret, totp_step(clock.now()))


async def turn_on(client: AsyncClient, clock: FrozenClock) -> TwoFactorAccount:
    """A new account with two-step sign-in on; the clock is moved on to a fresh code."""
    account = await register(client)
    setup = await client.post(f"{TOTP}/setup", headers=account.headers)
    assert setup.status_code == 200, setup.text
    secret = setup.json()["secret"]
    enabled = await client.post(
        f"{TOTP}/enable",
        headers=account.headers,
        json={"password": PASSWORD, "code": code_now(secret, clock)},
    )
    assert enabled.status_code == 200, enabled.text
    clock.advance(seconds=30)  # the code just used never works again: the next one will
    return TwoFactorAccount(account, secret, enabled.json()["recovery_codes"])
