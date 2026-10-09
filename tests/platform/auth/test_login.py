from __future__ import annotations

from argon2 import PasswordHasher as Argon2Hasher
from beanie import PydanticObjectId
from httpx import AsyncClient

from app.platform.users.models import User
from tests.helpers import (
    PASSWORD,
    FrozenClock,
    auth_settings,
    build_app,
    build_settings,
    login,
    register,
    running_client,
    unique_email,
)


async def test_login_returns_tokens_for_valid_credentials(client: AsyncClient) -> None:
    account = await register(client)

    response = await login(client, account.email.upper())

    assert response.status_code == 200
    assert response.json()["user"]["id"] == account.user_id
    assert response.cookies.get("tt_refresh")


async def test_wrong_password_and_unknown_email_look_identical(client: AsyncClient) -> None:
    account = await register(client)

    wrong_password = await login(client, account.email, "Wrong-Password-1")
    unknown_email = await login(client, unique_email("ghost"))

    for response in (wrong_password, unknown_email):
        assert response.status_code == 401
        assert response.json()["code"] == "AUTH_INVALID_CREDENTIALS"
        assert response.json()["message"] == "Email or password is incorrect."
        assert response.headers["WWW-Authenticate"] == "Bearer"


async def test_repeated_failures_lock_the_account_with_growing_backoff(mongo_uri: str) -> None:
    clock = FrozenClock()
    settings = build_settings(mongo_uri, auth=auth_settings(lockout_threshold=3))
    async with running_client(build_app(settings, clock)) as http:
        account = await register(http)
        for _ in range(3):
            assert (await login(http, account.email, "Wrong-Password-1")).status_code == 401

        locked = await login(http, account.email)  # even the right password is refused
        assert locked.status_code == 429
        assert locked.json()["code"] == "AUTH_ACCOUNT_LOCKED"
        assert locked.headers["Retry-After"] == "60"

        clock.advance(seconds=61)
        assert (await login(http, account.email, "Wrong-Password-1")).status_code == 401
        relocked = await login(http, account.email)
        assert relocked.status_code == 429
        assert relocked.headers["Retry-After"] == "120"  # 4th failure doubles the lock

        clock.advance(seconds=121)
        assert (await login(http, account.email)).status_code == 200
        user = await User.get(PydanticObjectId(account.user_id))
        assert user is not None
        assert user.failed_logins == 0
        assert user.locked_until is None


async def test_disabled_accounts_cannot_sign_in(client: AsyncClient, clock: FrozenClock) -> None:
    account = await register(client)
    await User.get_pymongo_collection().update_one(
        {"_id": PydanticObjectId(account.user_id)}, {"$set": {"disabled_at": clock.now()}}
    )

    response = await login(client, account.email)

    assert response.status_code == 403
    assert response.json()["code"] == "AUTH_ACCOUNT_DISABLED"


async def test_outdated_password_hashes_are_upgraded_on_login(client: AsyncClient) -> None:
    account = await register(client)
    legacy_hash = Argon2Hasher(time_cost=3, memory_cost=8192, parallelism=1).hash(PASSWORD)
    await User.get_pymongo_collection().update_one(
        {"_id": PydanticObjectId(account.user_id)}, {"$set": {"password_hash": legacy_hash}}
    )

    assert (await login(client, account.email)).status_code == 200

    user = await User.get(PydanticObjectId(account.user_id))
    assert user is not None
    assert user.password_hash != legacy_hash
    assert "t=1" in (user.password_hash or "")  # re-hashed with the configured cost
