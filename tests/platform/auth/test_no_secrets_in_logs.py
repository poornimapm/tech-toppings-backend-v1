from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.helpers import CSRF, PASSWORD, REFRESH_COOKIE, bearer, json_log_lines, login, register

NEW_PASSWORD = "Another-Secret-99"


async def test_passwords_and_tokens_never_reach_the_logs(
    capsys: pytest.CaptureFixture[str], client: AsyncClient
) -> None:
    capsys.readouterr()
    account = await register(client)
    first_refresh = client.cookies.get(REFRESH_COOKIE)
    await login(client, account.email, "Wrong-Password-77")
    signed_in = await login(client, account.email)
    refreshed = await client.post("/v1/auth/refresh", headers=CSRF)
    changed = await client.post(
        "/v1/auth/password/change",
        headers=bearer(refreshed.json()["access_token"]),
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )

    output = capsys.readouterr().out
    secrets = {
        "password": PASSWORD,
        "wrong password": "Wrong-Password-77",
        "new password": NEW_PASSWORD,
        "register access token": account.access_token,
        "login access token": signed_in.json()["access_token"],
        "refreshed access token": refreshed.json()["access_token"],
        "changed access token": changed.json()["access_token"],
        "refresh token": first_refresh or "",
        "rotated refresh token": refreshed.cookies.get(REFRESH_COOKIE) or "",
        "email": account.email,
    }
    leaked = [label for label, value in secrets.items() if value and value in output]
    assert leaked == []

    events = {event["event"] for event in json_log_lines(output)}
    assert {"auth_registered", "auth_login_failed", "auth_login_succeeded"} <= events
    assert "auth_password_changed" in events
