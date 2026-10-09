from __future__ import annotations

import pytest
from beanie import PydanticObjectId
from httpx import AsyncClient

from app.core.security import encode_jwt, epoch
from app.platform.users.models import User
from tests.helpers import TEST_JWT_SECRET, FrozenClock, bearer, register


async def test_profile_requires_a_bearer_token(client: AsyncClient) -> None:
    response = await client.get("/v1/me")

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHENTICATED"
    assert response.headers["WWW-Authenticate"] == "Bearer"


async def test_profile_with_valid_token(client: AsyncClient) -> None:
    account = await register(client, name="Ravi")

    response = await client.get("/v1/me", headers=account.headers)

    assert response.status_code == 200
    assert response.json()["name"] == "Ravi"
    assert response.json()["email"] == account.email


@pytest.mark.parametrize(
    "token",
    [
        "not-a-jwt",
        encode_jwt({"sub": "x", "exp": 4102444800}, "some-other-secret-" + "y" * 30),
    ],
)
async def test_malformed_or_foreign_tokens_are_invalid(client: AsyncClient, token: str) -> None:
    response = await client.get("/v1/me", headers=bearer(token))

    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_TOKEN_INVALID"


async def test_token_of_the_wrong_kind_is_invalid(client: AsyncClient, clock: FrozenClock) -> None:
    account = await register(client)
    now = epoch(clock.now())
    forged = encode_jwt(
        {
            "sub": account.user_id,
            "role": "admin",
            "tv": 0,
            "sid": "000000000000000000000000",
            "typ": "refresh",
            "jti": "abc",
            "iat": now,
            "exp": now + 600,
            "iss": "tech-toppings-api",
            "aud": "tech-toppings-web",
        },
        TEST_JWT_SECRET,
    )

    response = await client.get("/v1/me", headers=bearer(forged))

    assert response.json()["code"] == "AUTH_TOKEN_INVALID"


async def test_expired_tokens_are_rejected(client: AsyncClient, clock: FrozenClock) -> None:
    clock.advance(hours=-2)  # issue the token two hours in the past
    account = await register(client)
    clock.advance(hours=2)

    response = await client.get("/v1/me", headers=account.headers)

    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_TOKEN_EXPIRED"


async def test_tokens_of_disabled_users_stop_working(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await register(client)
    await User.get_pymongo_collection().update_one(
        {"_id": PydanticObjectId(account.user_id)}, {"$set": {"disabled_at": clock.now()}}
    )

    response = await client.get("/v1/me", headers=account.headers)

    assert response.json()["code"] == "AUTH_SESSION_REVOKED"


async def test_profile_update_changes_allowed_fields(client: AsyncClient) -> None:
    account = await register(client)

    response = await client.patch(
        "/v1/me",
        headers=account.headers,
        json={
            "name": "Asha K",
            "locale": "ta",
            "theme": "dark",
            "ui_mode": "simple",
            "base_currency": "USD",
            "timezone": "Europe/London",
            "ai_consent": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["name"], body["locale"], body["theme"], body["ui_mode"]) == (
        "Asha K",
        "ta",
        "dark",
        "simple",
    )
    assert (body["base_currency"], body["timezone"], body["ai_consent"]) == (
        "USD",
        "Europe/London",
        True,
    )


async def test_ai_consent_keeps_first_timestamp_and_can_be_withdrawn(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await register(client)
    await client.patch("/v1/me", headers=account.headers, json={"ai_consent": True})
    first = await User.get(PydanticObjectId(account.user_id))
    clock.advance(days=1)

    await client.patch("/v1/me", headers=account.headers, json={"ai_consent": True})
    again = await User.get(PydanticObjectId(account.user_id))
    withdrawn = await client.patch("/v1/me", headers=account.headers, json={"ai_consent": False})

    assert first is not None
    assert again is not None
    assert again.ai_consent_at == first.ai_consent_at
    assert withdrawn.json()["ai_consent"] is False


async def test_empty_update_is_a_no_op(client: AsyncClient) -> None:
    account = await register(client, name="Same")

    response = await client.patch("/v1/me", headers=account.headers, json={})

    assert response.status_code == 200
    assert response.json()["name"] == "Same"


@pytest.mark.parametrize(
    "payload",
    [
        {"timezone": "Mars/Olympus"},
        {"base_currency": "rupees"},
        {"locale": "fr"},
        {"role": "admin"},
        {"email": "new@example.com"},
        {"name": ""},
    ],
)
async def test_profile_update_rejects_invalid_or_protected_fields(
    client: AsyncClient, payload: dict[str, str]
) -> None:
    account = await register(client)

    response = await client.patch("/v1/me", headers=account.headers, json=payload)

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
