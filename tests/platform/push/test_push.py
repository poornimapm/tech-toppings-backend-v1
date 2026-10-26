"""``/v1/push``: only to browsers that agreed, only to real push services, readable only by the
browser it's for; a gone browser, or one subscribed under another key, is forgotten."""

from __future__ import annotations

import httpx
import pytest
import respx
from beanie import PydanticObjectId
from cryptography.hazmat.primitives.asymmetric import ec
from httpx import AsyncClient
from pydantic import SecretStr, ValidationError

from app.core.config import PushSettings, RateLimitSettings
from app.platform.push.models import PushSubscription
from app.platform.push.webpush import CURVE, b64encode, new_key_pair, public_bytes
from tests.helpers import FrozenClock, register
from tests.platform.push.conftest import (
    FCM,
    PRIVATE,
    PUBLIC,
    PUSH,
    Browser,
    devices,
    push_settings,
    subscribe,
)

# ---------------------------------------------------------------------- settings


def test_keys_come_as_a_pair() -> None:
    with pytest.raises(ValidationError, match="both"):
        PushSettings(_env_file=None, vapid_public_key=PUBLIC)
    assert not PushSettings(_env_file=None).available
    assert not push_settings(subject=None).available
    assert push_settings().available
    with pytest.raises(ValidationError):
        push_settings(subject="ops@example.com")  # mailto: or https: only


@pytest.mark.parametrize(
    ("public", "private"),
    [
        (new_key_pair()[0], PRIVATE),  # from two runs of `poe vapid-keys`
        (PRIVATE, PUBLIC),  # swapped
        (PUBLIC, "-----BEGIN EC PRIVATE KEY-----MHcCAQEEI"),  # a PEM pasted in
        (PUBLIC, b64encode(b"\xff" * 32)),  # not below the curve's order
        (PUBLIC + "A", PRIVATE),
    ],
)
def test_a_key_pair_that_does_not_fit_stops_startup(public: str, private: str) -> None:
    with pytest.raises(ValidationError, match="public key of") as caught:
        push_settings(vapid_public_key=public, vapid_private_key=SecretStr(private))
    assert private not in str(caught.value)  # a secret is never printed


def test_allowed_hosts_are_plain_lower_case_host_names() -> None:
    hosts = "FCM.googleapis.com, .push.apple.com, https://updates.push.services.mozilla.com/"
    assert push_settings(allowed_hosts=hosts).allowed_hosts == (
        "fcm.googleapis.com",
        "push.apple.com",
        "updates.push.services.mozilla.com",
    )
    for wrong in ("fcm.googleapis.com/fcm", "fcm.googleapis.com:443", "me@x.com", "a b.com", "com"):
        with pytest.raises(ValidationError, match="not a host name"):
            push_settings(allowed_hosts=wrong)


def test_a_browser_has_less_time_than_the_round() -> None:
    with pytest.raises(ValidationError, match="PUSH_DEVICE_TIMEOUT_SECONDS must be shorter"):
        push_settings(timeout_seconds=1.0, device_timeout_seconds=1.0)
    assert push_settings(timeout_seconds=1.0, device_timeout_seconds=0.9).available


def test_the_push_rate_is_checked_at_startup() -> None:
    with pytest.raises(ValidationError):
        RateLimitSettings(_env_file=None, push="ten/minute")


async def test_off_without_keys(client: AsyncClient) -> None:
    account = await register(client)
    assert (await client.get(PUSH, headers=account.headers)).json() == {
        "available": False,
        "public_key": None,
        "devices": 0,
    }
    refused = await subscribe(client, account, Browser("phone"))
    assert (refused.status_code, refused.json()["code"]) == (503, "PUSH_UNAVAILABLE")


# ---------------------------------------------------------------------- subscriptions


async def test_a_browser_is_subscribed_once_and_can_stop(pclient: AsyncClient) -> None:
    account, phone = await register(pclient), Browser("phone")
    state = (await subscribe(pclient, account, phone)).json()
    assert state == {"available": True, "public_key": PUBLIC, "devices": 1}
    assert (await subscribe(pclient, account, phone)).json()["devices"] == 1  # again: same one

    stopped = await pclient.request(
        "DELETE",
        f"{PUSH}/subscriptions",
        headers=account.headers,
        json={"endpoint": phone.endpoint},
    )
    assert stopped.status_code == 204
    assert await devices(pclient, account) == 0


async def test_a_shared_browser_follows_whoever_subscribed_it_last(pclient: AsyncClient) -> None:
    asha, bob, laptop = await register(pclient), await register(pclient), Browser("laptop")
    await subscribe(pclient, asha, laptop)
    await subscribe(pclient, bob, laptop)
    assert (await devices(pclient, asha), await devices(pclient, bob)) == (0, 1)


async def test_past_the_limit_the_oldest_browser_is_let_go(
    pclient: AsyncClient, clock: FrozenClock
) -> None:
    account = await register(pclient)
    for name in ("first", "second", "third"):
        await subscribe(pclient, account, Browser(name))
        clock.advance(seconds=1)
    kept = await PushSubscription.find({"endpoint": {"$regex": "/(first|second|third)$"}}).to_list()
    assert sorted(item.endpoint.rsplit("/", 1)[1] for item in kept) == ["second", "third"]


async def test_a_browser_taken_over_is_new_to_its_owner_and_never_let_go_at_once(
    pclient: AsyncClient, clock: FrozenClock
) -> None:
    asha, bob, laptop = await register(pclient), await register(pclient), Browser("shared")
    await subscribe(pclient, asha, laptop)  # the oldest of all
    clock.advance(seconds=1)
    await subscribe(pclient, bob, Browser("bob-1"))
    await subscribe(pclient, bob, Browser("bob-2"))  # bob is at the limit (2)

    assert (await subscribe(pclient, bob, laptop)).json()["devices"] == 2
    moved = await PushSubscription.find_one({"endpoint": laptop.endpoint})
    assert moved is not None
    assert moved.created_by == PydanticObjectId(bob.user_id)
    assert moved.created_at == clock.now()
    data = (await pclient.get("/v1/me/export", headers=bob.headers)).json()
    listed = {item["endpoint"]: item for item in data["collections"]["push_subscriptions"]}
    assert set(listed) == {laptop.endpoint, FCM + "bob-2"}
    assert listed[laptop.endpoint]["created_by"] == bob.user_id


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://evil.example.com/collect",
        "https://fcm.googleapis.com.evil.example.com/x",
        "https://fcm.googleapis.com./x",
    ],
)
async def test_only_real_push_services_are_sent_to(pclient: AsyncClient, endpoint: str) -> None:
    account, browser = await register(pclient), Browser("x")
    body = browser.subscription() | {"endpoint": endpoint}
    refused = await pclient.post(f"{PUSH}/subscriptions", headers=account.headers, json=body)
    assert (refused.status_code, refused.json()["code"]) == (422, "PUSH_ENDPOINT_NOT_ALLOWED")


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://[fcm.googleapis.com/x",
        "https://evil\N{FULLWIDTH NUMBER SIGN}.fcm.googleapis.com/x",
        "https://\N{ZERO WIDTH JOINER}.fcm.googleapis.com/x",
        "https://fcm.googleapis.com:abc/x",
        "https://fcm.googleapis.com:8443/fcm/send/x",
        "https://localhost:8000/v1/internal/jobs/all/run",
        "https://a:b@fcm.googleapis.com/fcm/send/x",
        "https://:b@fcm.googleapis.com/fcm/send/x",
        "https://fcm.googleapis.com/fcm/send/\x00",
        "https://fcm.googleapis.com/fcm/send/a b",
        "http://fcm.googleapis.com/fcm/send/x",
    ],
)
async def test_an_endpoint_no_push_service_gives_is_invalid(
    pclient: AsyncClient, endpoint: str
) -> None:
    account, browser = await register(pclient), Browser("x")
    body = browser.subscription() | {"endpoint": endpoint}
    refused = await pclient.post(f"{PUSH}/subscriptions", headers=account.headers, json=body)
    assert (refused.status_code, refused.json()["code"]) == (422, "VALIDATION_ERROR")


@pytest.mark.parametrize(
    "keys",
    [
        {"p256dh": b64encode(b"\x04" + b"\x01" * 64), "auth": b64encode(b"a" * 16)},  # off-curve
        {"p256dh": b64encode(public_bytes(ec.generate_private_key(CURVE))), "auth": "c2hvcnQ"},
    ],
)
async def test_keys_no_browser_makes_are_refused(
    pclient: AsyncClient, keys: dict[str, str]
) -> None:
    account = await register(pclient)
    body = {"endpoint": FCM + "x", "keys": keys}
    response = await pclient.post(f"{PUSH}/subscriptions", headers=account.headers, json=body)
    assert response.status_code == 422


async def test_an_export_lists_my_browsers_but_never_their_keys(pclient: AsyncClient) -> None:
    account, phone = await register(pclient), Browser("exported")
    response = await pclient.post(
        f"{PUSH}/subscriptions",
        headers=account.headers | {"User-Agent": "Mozilla/5.0 " + "x" * 600},  # any length
        json=phone.subscription(),
    )
    assert response.status_code == 200
    data = (await pclient.get("/v1/me/export", headers=account.headers)).json()
    [listed] = data["collections"]["push_subscriptions"]
    assert listed["endpoint"] == phone.endpoint
    assert not {"p256dh", "auth", "user_agent"} & set(listed)  # nor which browser it is


# ---------------------------------------------------------------------- sending


async def test_a_test_push_reaches_each_browser_readable_only_there(
    pclient: AsyncClient, push_service: respx.Route
) -> None:
    account, phone, laptop = await register(pclient), Browser("p1"), Browser("l1")
    await subscribe(pclient, account, phone)
    await subscribe(pclient, account, laptop)

    sent = await pclient.post(f"{PUSH}/test", headers=account.headers)
    assert sent.json() == {"sent": 2}
    by_endpoint = {str(call.request.url): call.request for call in push_service.calls}
    request = by_endpoint[phone.endpoint]
    assert phone.read(request.content) == {"type": "push.test", "params": {}}
    assert request.headers["Content-Encoding"] == "aes128gcm"
    assert request.headers["TTL"] == "86400"
    assert request.headers["Authorization"].startswith("vapid t=")
    assert request.headers["Authorization"].endswith(f", k={PUBLIC}")
    with pytest.raises(Exception):  # noqa: B017, PT011 - any failure: the key doesn't fit
        laptop.read(request.content)


async def test_a_gone_browser_is_forgotten_and_a_slow_one_kept(pclient: AsyncClient) -> None:
    account, gone, flaky = await register(pclient), Browser("gone"), Browser("flaky")
    await subscribe(pclient, account, gone)
    await subscribe(pclient, account, flaky)
    with respx.mock() as mock:
        mock.post(gone.endpoint).mock(return_value=httpx.Response(410))
        mock.post(flaky.endpoint).mock(side_effect=httpx.ConnectTimeout("slow"))
        assert (await pclient.post(f"{PUSH}/test", headers=account.headers)).json() == {"sent": 0}
    left = await PushSubscription.find(
        {"endpoint": {"$in": [gone.endpoint, flaky.endpoint]}}
    ).to_list()
    assert [item.endpoint for item in left] == [flaky.endpoint]  # tried again next time


async def test_a_browser_that_cannot_be_sent_to_never_stops_the_others(
    pclient: AsyncClient, push_service: respx.Route
) -> None:
    account, broken, phone = await register(pclient), Browser("broken"), Browser("fine")
    await subscribe(pclient, account, broken)
    await subscribe(pclient, account, phone)
    # Stored before endpoints were checked: a URL httpx won't send to.
    await PushSubscription.get_pymongo_collection().update_one(
        {"endpoint": broken.endpoint}, {"$set": {"endpoint": "https://fcm.googleapis.com:abc/x"}}
    )

    sent = await pclient.post(f"{PUSH}/test", headers=account.headers)

    assert sent.json() == {"sent": 1}
    assert [str(call.request.url) for call in push_service.calls] == [phone.endpoint]


async def test_browsers_subscribed_under_an_earlier_key_are_dropped(pclient: AsyncClient) -> None:
    account, phone, laptop = await register(pclient), Browser("rotated"), Browser("current")
    await subscribe(pclient, account, phone)
    await subscribe(pclient, account, laptop)
    await PushSubscription.get_pymongo_collection().update_one(
        {"endpoint": phone.endpoint}, {"$set": {"key_id": "made-with-old-key"}}
    )

    assert await devices(pclient, account) == 1  # the browser turns notifications on again
    left = await PushSubscription.find({"user_id": PydanticObjectId(account.user_id)}).to_list()
    assert [item.endpoint for item in left] == [laptop.endpoint]
