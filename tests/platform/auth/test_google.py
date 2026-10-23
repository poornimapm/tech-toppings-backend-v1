from __future__ import annotations

import time
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import AsyncClient
from jwt.algorithms import RSAAlgorithm

from app.core.config import RegistrationMode
from app.core.security import totp_code, totp_step
from tests.helpers import (
    FrozenClock,
    auth_settings,
    bearer,
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


@pytest.mark.usefixtures("google_keys")
async def test_two_step_sign_in_applies_to_google_too(mongo_uri: str) -> None:
    clock = FrozenClock(datetime.now(UTC) - timedelta(minutes=3))  # tokens never future-dated
    settings = build_settings(
        mongo_uri, auth=auth_settings(google_client_id=CLIENT_ID, google_jwks_url=JWKS_URL)
    )
    subject = f"two-step-{unique_email()}"

    def code() -> str:
        clock.advance(seconds=30)  # a fresh code each time: none works twice
        return totp_code(secret, totp_step(clock.now()))

    async with running_client(build_app(settings, clock)) as http:
        first = await http.post("/v1/auth/google", json={"id_token": id_token(sub=subject)})
        headers = bearer(first.json()["access_token"])
        secret = (await http.post("/v1/auth/totp/setup", headers=headers)).json()["secret"]
        # No password to prove it's them: a Google sign-in made just now instead.
        bare = await http.post("/v1/auth/totp/enable", headers=headers, json={"code": code()})
        assert bare.json()["code"] == "AUTH_GOOGLE_REAUTH_REQUIRED"
        on = await http.post(
            "/v1/auth/totp/enable",
            headers=headers,
            json={"code": code(), "google_id_token": id_token(sub=subject)},
        )
        assert on.status_code == 200, on.text

        asked = await http.post("/v1/auth/google", json={"id_token": id_token(sub=subject)})
        assert asked.json()["two_factor_required"] is True
        assert not asked.cookies.get("tt_refresh")
        done = await http.post(
            "/v1/auth/login/totp",
            json={"challenge_token": asked.json()["challenge_token"], "code": code()},
        )
        assert done.status_code == 200
        off = await http.post(
            "/v1/auth/totp/disable",
            headers=bearer(done.json()["access_token"]),
            json={"code": code(), "google_id_token": id_token(sub=subject)},
        )
        assert off.status_code == 204


@pytest.mark.usefixtures("google_keys")
async def test_a_google_only_account_sets_its_first_password_with_google_again(
    gclient: AsyncClient,
) -> None:
    subject = f"first-password-{unique_email()}"
    email = unique_email("first")
    first = await gclient.post(
        "/v1/auth/google", json={"id_token": id_token(sub=subject, email=email)}
    )
    headers = bearer(first.json()["access_token"])

    async def change(**body: Any) -> httpx.Response:
        return await gclient.post(
            "/v1/auth/password/change",
            headers=headers,
            json={"current_password": "anything", "new_password": "Brand-New-Secret-7", **body},
        )

    # A session alone could be stolen: a password set with it would open DELETE /v1/me.
    for refused in (await change(), await change(google_id_token=id_token(sub="someone-else"))):
        assert (refused.status_code, refused.json()["code"]) == (
            422,
            "AUTH_GOOGLE_REAUTH_REQUIRED",
        )
    assert (await change(google_id_token=id_token(sub=subject))).status_code == 200
    signed_in = await gclient.post(
        "/v1/auth/login", json={"email": email, "password": "Brand-New-Secret-7"}
    )
    assert signed_in.status_code == 200


@pytest.mark.usefixtures("google_keys")
async def test_a_google_only_account_signs_in_with_google_again_to_delete(
    gclient: AsyncClient,
) -> None:
    subject = f"leaving-{unique_email()}"
    email = unique_email("leaving")
    first = await gclient.post(
        "/v1/auth/google", json={"id_token": id_token(sub=subject, email=email)}
    )
    headers = bearer(first.json()["access_token"])

    async def delete(**body: Any) -> httpx.Response:
        return await gclient.request(
            "DELETE", "/v1/me", headers=headers, json={"confirm_email": email, **body}
        )

    minutes_ago = int(time.time()) - 600  # signed in long ago: not proof it's them now
    for refused in (
        await delete(),
        await delete(google_id_token=id_token(sub="someone-else")),
        await delete(google_id_token=id_token(sub=subject, iat=minutes_ago)),
    ):
        assert (refused.status_code, refused.json()["code"]) == (
            422,
            "AUTH_GOOGLE_REAUTH_REQUIRED",
        )
    assert (await delete(google_id_token=id_token(sub=subject))).status_code == 204
