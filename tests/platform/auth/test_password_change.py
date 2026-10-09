from __future__ import annotations

from beanie import PydanticObjectId
from fastapi import FastAPI
from httpx import AsyncClient

from app.core.security import PasswordHasher
from app.platform.auth.maintenance import reset_password
from app.platform.users.models import User
from tests.helpers import CSRF, PASSWORD, FrozenClock, bearer, login, register

NEW_PASSWORD = "Brand-New-Secret-7"


async def change(client: AsyncClient, token: str, current: str, new: str) -> dict[str, object]:
    response = await client.post(
        "/v1/auth/password/change",
        headers=bearer(token),
        json={"current_password": current, "new_password": new},
    )
    return {"status": response.status_code, **response.json()}


async def test_changing_password_signs_out_other_devices(
    client: AsyncClient, other_client: AsyncClient
) -> None:
    account = await register(client)
    assert (await login(other_client, account.email)).status_code == 200

    result = await change(client, account.access_token, PASSWORD, NEW_PASSWORD)

    assert result["status"] == 200
    assert (await other_client.post("/v1/auth/refresh", headers=CSRF)).status_code == 401
    assert (await client.get("/v1/me", headers=account.headers)).status_code == 401  # old token
    assert (
        await client.get("/v1/me", headers=bearer(str(result["access_token"])))
    ).status_code == 200
    assert (await client.post("/v1/auth/refresh", headers=CSRF)).status_code == 200  # new session
    assert (await login(client, account.email)).status_code == 401
    assert (await login(client, account.email, NEW_PASSWORD)).status_code == 200


async def test_wrong_current_password_is_a_422_not_a_401(client: AsyncClient) -> None:
    account = await register(client)

    result = await change(client, account.access_token, "Not-My-Password-1", NEW_PASSWORD)

    assert result["status"] == 422
    assert result["code"] == "AUTH_CURRENT_PASSWORD_INVALID"


async def test_new_password_must_follow_policy_and_differ(client: AsyncClient) -> None:
    account = await register(client)

    weak = await change(client, account.access_token, PASSWORD, "short")
    same = await change(client, account.access_token, PASSWORD, PASSWORD)

    assert weak["code"] == "AUTH_PASSWORD_POLICY"
    assert same["code"] == "AUTH_PASSWORD_POLICY"
    assert same["details"] == {
        "violations": ["same_as_current"],
        "min_length": 10,
        "min_classes": 2,
    }


async def test_admin_reset_forces_a_password_change_before_anything_else(
    app: FastAPI, client: AsyncClient, clock: FrozenClock
) -> None:
    account = await register(client)
    one_time = await reset_password(
        account.email, app.state.settings, clock, PasswordHasher(app.state.settings.auth)
    )

    signed_in = await login(client, account.email, one_time)
    token = signed_in.json()["access_token"]
    profile = await client.get("/v1/me", headers=bearer(token))
    blocked = await client.patch("/v1/me", headers=bearer(token), json={"name": "X"})
    sessions_blocked = await client.get("/v1/me/sessions", headers=bearer(token))
    changed = await change(client, token, one_time, NEW_PASSWORD)
    unblocked = await client.patch(
        "/v1/me", headers=bearer(str(changed["access_token"])), json={"name": "X"}
    )

    assert signed_in.status_code == 200
    assert signed_in.json()["user"]["must_change_password"] is True
    assert profile.status_code == 200
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "AUTH_PASSWORD_CHANGE_REQUIRED"
    assert sessions_blocked.status_code == 403
    assert changed["status"] == 200
    assert unblocked.status_code == 200
    assert unblocked.json()["must_change_password"] is False


async def test_accounts_without_a_password_can_set_one(client: AsyncClient) -> None:
    account = await register(client)
    await User.get_pymongo_collection().update_one(
        {"_id": PydanticObjectId(account.user_id)}, {"$set": {"password_hash": None}}
    )

    result = await change(client, account.access_token, "anything", NEW_PASSWORD)

    assert result["status"] == 200
    assert (await login(client, account.email, NEW_PASSWORD)).status_code == 200
