from __future__ import annotations

import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import AsyncClient
from jwt.algorithms import RSAAlgorithm

from app.core.config import RegistrationMode
from tests.helpers import (
    auth_settings,
    build_app,
    build_settings,
    register,
    running_client,
    unique_email,
)

CLIENT_ID = "test-client.apps.googleusercontent.com"
JWKS_URL = "https://keys.example.test/oauth2/v3/certs"
KID = "test-key-1"

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwks() -> dict[str, Any]:
    public = RSAAlgorithm.to_jwk(_PRIVATE_KEY.public_key(), as_dict=True)
    return {"keys": [{**public, "kid": KID, "alg": "RS256", "use": "sig"}]}


def id_token(**overrides: Any) -> str:
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "google-subject-1",
        "email": unique_email("g"),
        "email_verified": True,
        "name": "Gita G",
        "iat": now,
        "exp": now + 600,
    } | overrides
    kid = claims.pop("_kid", KID)
    return jwt.encode(claims, _PRIVATE_KEY, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def google_keys() -> Iterator[respx.Route]:
    with respx.mock(assert_all_called=False) as mock:
        yield mock.get(JWKS_URL).mock(return_value=httpx.Response(200, json=jwks()))


async def google_client(
    mongo_uri: str, mode: RegistrationMode = RegistrationMode.OPEN
) -> AsyncIterator[AsyncClient]:
    settings = build_settings(
        mongo_uri,
        auth=auth_settings(
            google_client_id=CLIENT_ID, google_jwks_url=JWKS_URL, registration_mode=mode
        ),
    )
    async with running_client(build_app(settings)) as http:
        yield http


@pytest.fixture
async def gclient(mongo_uri: str) -> AsyncIterator[AsyncClient]:
    async for http in google_client(mongo_uri):
        yield http


async def test_google_sign_in_is_disabled_without_a_client_id(client: AsyncClient) -> None:
    response = await client.post("/v1/auth/google", json={"id_token": id_token()})

    assert response.status_code == 404
    assert response.json()["code"] == "AUTH_GOOGLE_DISABLED"


@pytest.mark.usefixtures("google_keys")
async def test_first_sign_in_creates_a_google_only_account(gclient: AsyncClient) -> None:
    subject = f"sub-{unique_email()}"

    first = await gclient.post("/v1/auth/google", json={"id_token": id_token(sub=subject)})
    again = await gclient.post("/v1/auth/google", json={"id_token": id_token(sub=subject)})

    assert first.status_code == 200
    user = first.json()["user"]
    assert user["auth_providers"] == ["google"]
    assert user["has_password"] is False
    assert user["name"] == "Gita G"
    assert again.json()["user"]["id"] == user["id"]  # found by Google subject, not duplicated
    config = (await gclient.get("/v1/auth/config")).json()
    assert config["google_client_id"] == CLIENT_ID


@pytest.mark.usefixtures("google_keys")
async def test_existing_account_with_the_same_email_is_linked(gclient: AsyncClient) -> None:
    account = await register(gclient)

    response = await gclient.post(
        "/v1/auth/google",
        json={"id_token": id_token(sub=f"s-{account.user_id}", email=account.email)},
    )

    assert response.json()["user"]["id"] == account.user_id
    assert response.json()["user"]["auth_providers"] == ["google"]
    assert response.json()["user"]["has_password"] is True


@pytest.mark.usefixtures("google_keys")
async def test_allowlist_applies_to_new_google_accounts(mongo_uri: str) -> None:
    async for http in google_client(mongo_uri, RegistrationMode.ALLOWLIST):
        response = await http.post("/v1/auth/google", json={"id_token": id_token(sub="stranger")})

        assert response.status_code == 403
        assert response.json()["code"] == "AUTH_REGISTRATION_NOT_ALLOWED"


@pytest.mark.usefixtures("google_keys")
@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "someone-else.apps.googleusercontent.com"},
        {"iss": "https://evil.example.com"},
        {"exp": int(time.time()) - 60},
        {"email_verified": False},
        {"_kid": "unknown-key"},
    ],
)
async def test_untrustworthy_tokens_are_rejected(
    gclient: AsyncClient, overrides: dict[str, Any]
) -> None:
    response = await gclient.post("/v1/auth/google", json={"id_token": id_token(**overrides)})

    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_GOOGLE_TOKEN_INVALID"


async def test_unreachable_google_keys_fail_closed(gclient: AsyncClient) -> None:
    with respx.mock() as mock:
        mock.get(JWKS_URL).mock(return_value=httpx.Response(503))
        response = await gclient.post("/v1/auth/google", json={"id_token": id_token()})

    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_GOOGLE_TOKEN_INVALID"


async def test_keys_are_cached_between_sign_ins(
    gclient: AsyncClient, google_keys: respx.Route
) -> None:
    for _ in range(3):
        await gclient.post("/v1/auth/google", json={"id_token": id_token(sub="cached-user")})

    assert google_keys.call_count == 1
