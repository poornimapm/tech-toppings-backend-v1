"""``/v1/admin/allowed-emails``: who may register while registration is by allowlist, from the
settings (read only here) and from the console, each change audited."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient

from app.core.config import RegistrationMode
from app.platform.users.models import User
from tests.helpers import (
    PASSWORD,
    Account,
    auth_settings,
    build_app,
    build_settings,
    login,
    register,
    running_client,
)
from tests.platform.admin.conftest import admin_account, audit_trail

ALLOWED = "/v1/admin/allowed-emails"
FROM_SETTINGS = "listed@example.com"


@pytest.fixture
async def allowlist_client(own_mongo_uri: str) -> AsyncIterator[AsyncClient]:
    auth = auth_settings(
        registration_mode=RegistrationMode.ALLOWLIST, allowed_emails=(FROM_SETTINGS,)
    )
    async with running_client(build_app(build_settings(own_mongo_uri, auth=auth))) as client:
        yield client


async def sign_up(client: AsyncClient, email: str) -> int:
    response = await client.post(
        "/v1/auth/register", json={"email": email, "name": "New", "password": PASSWORD}
    )
    return response.status_code


async def admin_from_settings(client: AsyncClient) -> Account:
    """An admin, registered from the settings' list."""
    assert await sign_up(client, FROM_SETTINGS) == 201
    user = await User.find_one({"email": FROM_SETTINGS})
    assert user is not None
    await User.get_pymongo_collection().update_one({"_id": user.id}, {"$set": {"role": "admin"}})
    token = (await login(client, FROM_SETTINGS)).json()["access_token"]
    return Account(FROM_SETTINGS, PASSWORD, token, str(user.id))


async def test_an_address_added_in_the_console_may_register(allowlist_client: AsyncClient) -> None:
    client = allowlist_client
    admin = await admin_from_settings(client)
    assert await sign_up(client, "asha@example.com") == 403

    added = await client.post(
        ALLOWED, headers=admin.headers, json={"email": "Asha@Example.com", "note": "family"}
    )
    assert added.status_code == 201, added.text
    assert (added.json()["email"], added.json()["source"]) == ("asha@example.com", "admin")
    assert await sign_up(client, "asha@example.com") == 201

    listed = (await client.get(ALLOWED, headers=admin.headers)).json()
    assert [(item["email"], item["source"]) for item in listed] == [
        (FROM_SETTINGS, "settings"),
        ("asha@example.com", "admin"),
    ]
    again = await client.post(ALLOWED, headers=admin.headers, json={"email": "asha@example.com"})
    assert (again.status_code, again.json()["code"]) == (409, "ALLOWLIST_EXISTS")


async def test_a_removed_address_may_no_longer_register(allowlist_client: AsyncClient) -> None:
    client = allowlist_client
    admin = await admin_from_settings(client)
    await client.post(ALLOWED, headers=admin.headers, json={"email": "ravi@example.com"})
    removed = await client.delete(f"{ALLOWED}/ravi@example.com", headers=admin.headers)
    assert removed.status_code == 204
    assert await sign_up(client, "ravi@example.com") == 403
    missing = await client.delete(f"{ALLOWED}/ravi@example.com", headers=admin.headers)
    assert missing.status_code == 404
    settings_one = await client.delete(f"{ALLOWED}/{FROM_SETTINGS}", headers=admin.headers)
    assert (settings_one.status_code, settings_one.json()["code"]) == (409, "ALLOWLIST_IN_SETTINGS")

    trail = await audit_trail(client, admin, entity="allowed_email")
    assert [(item["action"], item["entity_id"]) for item in trail] == [
        ("allowlist.removed", "ravi@example.com"),
        ("allowlist.added", "ravi@example.com"),
    ]


async def test_only_admins_see_the_list(client: AsyncClient) -> None:
    user = await register(client)
    assert (await client.get(ALLOWED, headers=user.headers)).status_code == 403
    admin = await admin_account(client)
    assert (await client.get(ALLOWED, headers=admin.headers)).status_code == 200
