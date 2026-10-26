"""A browser gets pushes only while the device that turned them on is signed in: signing out
there, from another device, everywhere, or by changing the password ends them, and so does a
session that ended any other way."""

from __future__ import annotations

from datetime import timedelta

import pytest
import respx
from beanie import PydanticObjectId
from httpx import AsyncClient

from app.core.config import Settings
from app.platform.auth.models import AuthSession
from app.platform.push.models import PushSubscription
from tests.helpers import CSRF, PASSWORD, Account, account_from, build_settings, login, register
from tests.platform.push.conftest import PUSH, Browser, devices, push_settings, subscribe


@pytest.fixture
def settings(mongo_uri: str) -> Settings:
    return build_settings(mongo_uri, push=push_settings())


async def on_two_devices(
    client: AsyncClient, other_client: AsyncClient, name: str
) -> tuple[Account, Account, Browser, Browser]:
    """One account signed in on a phone and a laptop, each browser receiving pushes."""
    phone_account = await register(client)
    response = await login(other_client, phone_account.email)
    laptop_account = account_from(response, phone_account.email, PASSWORD)
    phone, laptop = Browser(f"{name}-phone"), Browser(f"{name}-laptop")
    assert (await subscribe(client, phone_account, phone)).status_code == 200
    assert (await subscribe(other_client, laptop_account, laptop)).json()["devices"] == 2
    return phone_account, laptop_account, phone, laptop


async def receiving(account: Account) -> list[str]:
    found = await PushSubscription.find({"user_id": PydanticObjectId(account.user_id)}).to_list()
    return sorted(item.endpoint for item in found)


async def test_signing_out_ends_that_devices_pushes(
    client: AsyncClient, other_client: AsyncClient
) -> None:
    phone_account, _, phone, _ = await on_two_devices(client, other_client, "logout")

    assert (await other_client.post("/v1/auth/logout", headers=CSRF)).status_code == 204

    assert await receiving(phone_account) == [phone.endpoint]


async def test_signing_a_device_out_from_another_ends_its_pushes(
    client: AsyncClient, other_client: AsyncClient
) -> None:
    phone_account, _, phone, _ = await on_two_devices(client, other_client, "revoke")
    listed = (await client.get("/v1/me/sessions", headers=phone_account.headers)).json()
    [laptop_session] = [item["id"] for item in listed["items"] if not item["current"]]

    revoked = await client.delete(
        f"/v1/me/sessions/{laptop_session}", headers=phone_account.headers
    )

    assert revoked.status_code == 204
    assert await receiving(phone_account) == [phone.endpoint]


async def test_signing_out_everywhere_ends_every_devices_pushes(
    client: AsyncClient, other_client: AsyncClient
) -> None:
    phone_account, *_ = await on_two_devices(client, other_client, "everywhere")

    ended = await client.post("/v1/auth/logout-all", headers=phone_account.headers)

    assert ended.status_code == 204
    assert await receiving(phone_account) == []


async def test_a_password_change_ends_every_devices_pushes(
    client: AsyncClient, other_client: AsyncClient
) -> None:
    phone_account, *_ = await on_two_devices(client, other_client, "password")

    changed = await client.post(
        "/v1/auth/password/change",
        headers=phone_account.headers,
        json={"current_password": PASSWORD, "new_password": "Another-Horse-43"},
    )

    assert changed.status_code == 200, changed.text
    assert await receiving(phone_account) == []  # this browser turns them on again


async def test_a_device_whose_session_ended_is_never_pushed_to(
    client: AsyncClient, other_client: AsyncClient, push_service: respx.Route
) -> None:
    phone_account, laptop_account, phone, laptop = await on_two_devices(
        client, other_client, "ended"
    )
    laptop_row = await PushSubscription.find_one({"endpoint": laptop.endpoint})
    assert laptop_row is not None
    # Its refresh session ran out (or ended in a way that didn't clean up after itself).
    await AuthSession.get_pymongo_collection().update_one(
        {"_id": laptop_row.session_id},
        {"$set": {"expires_at": laptop_row.created_at - timedelta(seconds=1)}},
    )

    sent = await client.post(f"{PUSH}/test", headers=phone_account.headers)

    assert sent.json() == {"sent": 1}
    assert [str(call.request.url) for call in push_service.calls] == [phone.endpoint]
    assert await receiving(laptop_account) == [phone.endpoint]  # the laptop's is forgotten


async def test_a_browser_not_tied_to_a_device_is_dropped(
    client: AsyncClient, other_client: AsyncClient
) -> None:
    phone_account, _, phone, laptop = await on_two_devices(client, other_client, "untied")
    await PushSubscription.get_pymongo_collection().update_one(
        {"endpoint": laptop.endpoint},
        {"$set": {"session_id": None}},  # stored before sessions
    )

    assert await devices(client, phone_account) == 1
    assert await receiving(phone_account) == [phone.endpoint]
