from __future__ import annotations

import pytest
from httpx import AsyncClient
from pydantic import SecretStr

from app.core.config import BootstrapSettings, RegistrationMode
from tests.helpers import (
    PASSWORD,
    REFRESH_COOKIE,
    auth_settings,
    build_app,
    build_settings,
    register,
    running_client,
    unique_email,
)


async def test_register_creates_account_with_defaults_and_signs_in(client: AsyncClient) -> None:
    email = unique_email("Asha").upper()

    response = await client.post(
        "/v1/auth/register", json={"email": email, "name": "  Asha  ", "password": PASSWORD}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 900
    assert set(body) == {"access_token", "token_type", "expires_in", "user"}
    refresh_token = response.cookies.get(REFRESH_COOKIE)
    assert refresh_token
    assert refresh_token not in response.text  # the refresh token lives only in the cookie
    user = body["user"]
    assert user["email"] == email.lower()
    assert user["name"] == "Asha"
    assert user["role"] == "user"
    assert user["locale"] == "en"
    assert user["base_currency"] == "INR"
    assert user["timezone"] == "Asia/Kolkata"
    assert user["has_password"] is True
    assert user["must_change_password"] is False
    assert user["ai_consent"] is False


async def test_refresh_cookie_is_http_only_and_scoped(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/auth/register", json={"email": unique_email(), "name": "A", "password": PASSWORD}
    )

    [cookie] = response.headers.get_list("set-cookie")
    assert cookie.startswith(f"{REFRESH_COOKIE}=")
    assert "HttpOnly" in cookie
    assert "Path=/v1/auth" in cookie
    assert "SameSite=strict" in cookie
    assert "Max-Age=2592000" in cookie  # 30-day sliding window


async def test_email_must_be_unique_case_insensitively(client: AsyncClient) -> None:
    account = await register(client)

    response = await client.post(
        "/v1/auth/register",
        json={"email": account.email.upper(), "name": "Twin", "password": PASSWORD},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "AUTH_EMAIL_TAKEN"


@pytest.mark.parametrize(
    ("password", "violations"),
    [
        ("Short1!", ["too_short"]),
        ("alllowercaseletters", ["too_simple"]),
        (" Padded-Password-1 ", ["surrounding_whitespace"]),
    ],
)
async def test_password_policy_is_enforced(
    client: AsyncClient, password: str, violations: list[str]
) -> None:
    response = await client.post(
        "/v1/auth/register", json={"email": unique_email(), "name": "A", "password": password}
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "AUTH_PASSWORD_POLICY"
    assert body["details"]["violations"] == violations
    assert body["details"]["min_length"] == 10


async def test_invalid_email_and_unknown_fields_are_rejected(client: AsyncClient) -> None:
    bad_email = await client.post(
        "/v1/auth/register", json={"email": "not-an-email", "name": "A", "password": PASSWORD}
    )
    escalation = await client.post(
        "/v1/auth/register",
        json={"email": unique_email(), "name": "A", "password": PASSWORD, "role": "admin"},
    )

    assert bad_email.status_code == 422
    assert bad_email.json()["code"] == "VALIDATION_ERROR"
    assert escalation.status_code == 422
    assert ["body", "role"] in [error["loc"] for error in escalation.json()["details"]]


async def test_allowlist_mode_admits_only_listed_and_bootstrap_emails(mongo_uri: str) -> None:
    invited = unique_email("invited")
    admin = unique_email("admin")
    settings = build_settings(
        mongo_uri,
        auth=auth_settings(registration_mode=RegistrationMode.ALLOWLIST, allowed_emails=(invited,)),
        bootstrap=BootstrapSettings(
            _env_file=None, email=admin, password=SecretStr("Admin-Password-1")
        ),
    )
    async with running_client(build_app(settings)) as http:
        allowed = await http.post(
            "/v1/auth/register",
            json={"email": invited.upper(), "name": "In", "password": PASSWORD},
        )
        stranger = await http.post(
            "/v1/auth/register", json={"email": unique_email(), "name": "X", "password": PASSWORD}
        )
        bootstrap = await http.post(
            "/v1/auth/register", json={"email": admin, "name": "Admin", "password": PASSWORD}
        )
        config = (await http.get("/v1/auth/config")).json()

    assert allowed.status_code == 201
    assert stranger.status_code == 403
    assert stranger.json()["code"] == "AUTH_REGISTRATION_NOT_ALLOWED"
    assert bootstrap.status_code == 201
    assert bootstrap.json()["user"]["role"] == "user"  # promotion happens only via `poe seed`
    assert config["registration_mode"] == "allowlist"


async def test_closed_mode_rejects_everyone(mongo_uri: str) -> None:
    settings = build_settings(
        mongo_uri, auth=auth_settings(registration_mode=RegistrationMode.CLOSED)
    )
    async with running_client(build_app(settings)) as http:
        response = await http.post(
            "/v1/auth/register", json={"email": unique_email(), "name": "X", "password": PASSWORD}
        )

    assert response.status_code == 403
    assert response.json()["code"] == "AUTH_REGISTRATION_CLOSED"


async def test_auth_config_is_public_and_describes_sign_in(client: AsyncClient) -> None:
    response = await client.get("/v1/auth/config")

    assert response.status_code == 200
    assert response.json() == {
        "registration_mode": "open",
        "google_client_id": None,
        "password_policy": {"min_length": 10, "max_length": 128, "min_classes": 2},
        "csrf_header": "X-TT-CSRF",
    }
