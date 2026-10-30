"""``/v1/admin/users``: accounts as an admin manages them (role, disable, sign out everywhere,
reset a password), each change audited, and never the admin's own account."""

from __future__ import annotations

import asyncio

from beanie import PydanticObjectId
from httpx import AsyncClient

from app.platform.admin.accounts import ACTIVE_ADMINS
from app.platform.users.models import User
from tests.helpers import FrozenClock, build_app, build_settings, login, register, running_client
from tests.platform.admin.conftest import admin_account, audit_trail

USERS = "/v1/admin/users"


async def test_lists_finds_and_reads_accounts(client: AsyncClient) -> None:
    admin = await admin_account(client)
    priya = await register(client, name="Priya Raman")

    found = (
        await client.get(USERS, headers=admin.headers, params={"q": "raman", "page_size": 50})
    ).json()
    assert [item["email"] for item in found["items"]] == [priya.email]
    assert found["items"][0] | {"created_at": None, "last_login_at": None} == {
        "id": priya.user_id,
        "email": priya.email,
        "name": "Priya Raman",
        "role": "user",
        "created_at": None,
        "last_login_at": None,
        "disabled": False,
        "must_change_password": False,
        "two_factor": False,
        "locked": False,
        "providers": [],
        "modules": [],
    }
    one = (await client.get(f"{USERS}/{priya.user_id}", headers=admin.headers)).json()
    assert one["live_sessions"] == 1
    admins = (
        await client.get(USERS, headers=admin.headers, params={"role": "admin", "page_size": 100})
    ).json()
    assert admin.email in [item["email"] for item in admins["items"]]


async def test_a_role_change_is_audited_and_signs_the_account_in_again(client: AsyncClient) -> None:
    admin = await admin_account(client)
    ravi = await register(client)

    changed = await client.patch(
        f"{USERS}/{ravi.user_id}", headers=admin.headers, json={"role": "admin"}
    )
    assert (changed.status_code, changed.json()["role"]) == (200, "admin")
    assert (await client.get("/v1/me", headers=ravi.headers)).status_code == 401  # old token
    trail = await audit_trail(client, admin, owner_id=ravi.user_id)
    assert [(item["action"], item["details"]) for item in trail] == [
        ("user.role_changed", {"from": "user", "to": "admin"})
    ]
    assert (trail[0]["actor_email"], trail[0]["owner_email"]) == (admin.email, ravi.email)
    assert trail[0]["request_id"]
    assert trail[0]["ip"]


async def test_a_disabled_account_can_not_sign_in_until_enabled(client: AsyncClient) -> None:
    admin = await admin_account(client)
    ravi = await register(client)

    off = await client.patch(
        f"{USERS}/{ravi.user_id}", headers=admin.headers, json={"disabled": True}
    )
    assert off.json()["disabled"] is True
    assert (await client.get("/v1/me", headers=ravi.headers)).status_code == 401
    assert (await login(client, ravi.email)).status_code == 403
    on = await client.patch(
        f"{USERS}/{ravi.user_id}", headers=admin.headers, json={"disabled": False}
    )
    assert on.json()["disabled"] is False
    assert (await login(client, ravi.email)).status_code == 200
    actions = [item["action"] for item in await audit_trail(client, admin, owner_id=ravi.user_id)]
    assert actions == ["user.enabled", "user.disabled"]

    unchanged = await client.patch(
        f"{USERS}/{ravi.user_id}", headers=admin.headers, json={"disabled": False}
    )
    assert unchanged.status_code == 200
    assert len(await audit_trail(client, admin, owner_id=ravi.user_id)) == 2  # nothing changed


async def test_signs_an_account_out_everywhere(client: AsyncClient) -> None:
    admin = await admin_account(client)
    ravi = await register(client)
    await login(client, ravi.email)  # a second device

    revoked = await client.post(f"{USERS}/{ravi.user_id}/revoke-sessions", headers=admin.headers)
    assert revoked.json() == {"sessions": 2}
    assert (await client.get("/v1/me", headers=ravi.headers)).status_code == 401
    trail = await audit_trail(client, admin, action="user.sessions_revoked")
    assert trail[0]["details"] == {"sessions": 2}


async def test_a_reset_gives_a_one_time_password_once_and_clears_2fa(client: AsyncClient) -> None:
    admin = await admin_account(client)
    ravi = await register(client)
    await User.get_pymongo_collection().update_one(
        {"_id": PydanticObjectId(ravi.user_id)},
        {
            "$set": {
                "totp_secret": "encrypted",
                "totp_enabled_at": "2026-10-01T00:00:00Z",
                "recovery_codes": ["x"],
            }
        },
    )

    reset = await client.post(f"{USERS}/{ravi.user_id}/reset-password", headers=admin.headers)
    one_time = reset.json()["one_time_password"]
    assert len(one_time) == 19  # four readable groups of four, with dashes
    assert (await login(client, ravi.email)).status_code == 401  # the old password
    again = await login(client, ravi.email, one_time)
    assert again.status_code == 200
    assert again.json()["user"]["must_change_password"] is True
    user = await User.get(PydanticObjectId(ravi.user_id))
    assert user is not None
    assert (user.totp_secret, user.totp_enabled_at, user.recovery_codes) == (None, None, [])

    trail = await audit_trail(client, admin, action="user.password_reset")
    assert trail[0]["details"] == {"two_factor_cleared": True}
    assert one_time not in str(trail)  # never written down


async def test_an_admin_does_not_change_their_own_account_here(client: AsyncClient) -> None:
    admin = await admin_account(client)
    for call in (
        client.patch(f"{USERS}/{admin.user_id}", headers=admin.headers, json={"role": "user"}),
        client.post(f"{USERS}/{admin.user_id}/revoke-sessions", headers=admin.headers),
        client.post(f"{USERS}/{admin.user_id}/reset-password", headers=admin.headers),
    ):
        response = await call
        assert (response.status_code, response.json()["code"]) == (409, "ADMIN_SELF_CHANGE")


async def test_two_admins_demoting_each_other_at_once_leave_one(
    own_mongo_uri: str, clock: FrozenClock
) -> None:
    async with running_client(build_app(build_settings(own_mongo_uri), clock)) as client:
        first, second = await admin_account(client), await admin_account(client)

        answers = await asyncio.gather(
            client.patch(f"{USERS}/{second.user_id}", headers=first.headers, json={"role": "user"}),
            client.patch(f"{USERS}/{first.user_id}", headers=second.headers, json={"role": "user"}),
        )

        assert {answer.status_code for answer in answers} <= {200, 401, 403, 409}
        admins = await User.find(ACTIVE_ADMINS).count()
        assert admins >= 1
        refused = [answer.json()["code"] for answer in answers if answer.status_code == 409]
        assert set(refused) <= {"ADMIN_LAST_ADMIN"}


async def test_an_unknown_account_is_not_found(client: AsyncClient) -> None:
    admin = await admin_account(client)
    missing = str(PydanticObjectId())
    assert (await client.get(f"{USERS}/{missing}", headers=admin.headers)).status_code == 404
    patched = await client.patch(
        f"{USERS}/{missing}", headers=admin.headers, json={"role": "admin"}
    )
    assert patched.status_code == 404
    empty = await client.patch(f"{USERS}/{missing}", headers=admin.headers, json={})
    assert empty.status_code == 422
