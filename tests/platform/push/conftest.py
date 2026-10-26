"""Helpers for push tests: a server with VAPID keys, browsers with their own push keys, and a
push service that takes every message."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import ec
from httpx import AsyncClient
from pydantic import SecretStr

from app.core.config import PushSettings
from app.platform.push.webpush import CURVE, b64encode, new_key_pair, public_bytes
from tests.helpers import Account, FrozenClock, build_app, build_settings, running_client
from tests.platform.push.test_webpush import decrypt

PUSH = "/v1/push"
FCM = "https://fcm.googleapis.com/fcm/send/"
PUBLIC, PRIVATE = new_key_pair()


def push_settings(**changes: Any) -> PushSettings:
    values: dict[str, Any] = {
        "vapid_public_key": PUBLIC,
        "vapid_private_key": SecretStr(PRIVATE),
        "subject": "mailto:ops@example.com",
    }
    return PushSettings(_env_file=None, **(values | changes))


class Browser:
    """A browser's push keys: it alone can read what is pushed to it."""

    def __init__(self, name: str) -> None:
        self.key = ec.generate_private_key(CURVE)
        self.auth = name.encode().ljust(16, b"-")[:16]
        self.endpoint = FCM + name

    def subscription(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "expirationTime": None,
            "keys": {"p256dh": b64encode(public_bytes(self.key)), "auth": b64encode(self.auth)},
        }

    def read(self, body: bytes) -> dict[str, Any]:
        return dict(json.loads(decrypt(body, self.key, self.auth)))


@pytest.fixture
async def pclient(mongo_uri: str, clock: FrozenClock) -> AsyncIterator[AsyncClient]:
    settings = build_settings(mongo_uri, push=push_settings(max_subscriptions=2))
    async with running_client(build_app(settings, clock)) as http:
        yield http


@pytest.fixture
def push_service() -> Iterator[respx.Route]:
    with respx.mock(assert_all_called=False) as mock:
        yield mock.post(url__startswith=FCM).mock(return_value=httpx.Response(201))


async def subscribe(client: AsyncClient, account: Account, browser: Browser) -> httpx.Response:
    return await client.post(
        f"{PUSH}/subscriptions", headers=account.headers, json=browser.subscription()
    )


async def devices(client: AsyncClient, account: Account) -> int:
    return int((await client.get(PUSH, headers=account.headers)).json()["devices"])


def reached(route: respx.Route, browser: Browser) -> list[dict[str, Any]]:
    """What ``browser`` was pushed, read with its own keys."""
    calls = [call for call in route.calls if str(call.request.url) == browser.endpoint]
    return [browser.read(call.request.content) for call in calls]
