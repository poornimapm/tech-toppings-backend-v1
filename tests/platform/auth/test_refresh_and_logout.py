from __future__ import annotations

from beanie import PydanticObjectId
from httpx import AsyncClient, Response

from app.platform.auth.models import AuthSession
from app.platform.users.models import User
from tests.helpers import (
    CSRF,
    REFRESH_COOKIE,
    FrozenClock,
    auth_settings,
    build_app,
    build_settings,
    login,
    register,
    running_client,
)


async def refresh_with(client: AsyncClient, token: str) -> Response:
    """Present a specific refresh token (an explicit Cookie header bypasses the jar)."""
    return await client.post(
        "/v1/auth/refresh", headers={**CSRF, "Cookie": f"{REFRESH_COOKIE}={token}"}
    )


async def test_refresh_rotates_the_cookie_and_issues_a_new_access_token(
    client: AsyncClient,
) -> None:
    account = await register(client)
    first_cookie = client.cookies.get(REFRESH_COOKIE)

    response = await client.post("/v1/auth/refresh", headers=CSRF)

    assert response.status_code == 200
    assert response.json()["user"]["id"] == account.user_id
    second_cookie = response.cookies.get(REFRESH_COOKIE)
    assert second_cookie
    assert second_cookie != first_cookie
    me = await client.get(
        "/v1/me", headers={"Authorization": f"Bearer {response.json()['access_token']}"}
    )
    assert me.status_code == 200


async def test_refresh_requires_the_csrf_header(client: AsyncClient) -> None:
    await register(client)

    response = await client.post("/v1/auth/refresh")

    assert response.status_code == 403
    assert response.json()["code"] == "AUTH_CSRF_FAILED"


async def test_refresh_without_a_cookie_is_rejected(other_client: AsyncClient) -> None:
    response = await other_client.post("/v1/auth/refresh", headers=CSRF)

    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_REFRESH_INVALID"


async def test_reusing_an_old_refresh_token_revokes_the_session(
    client: AsyncClient, other_client: AsyncClient, clock: FrozenClock
) -> None:
    await register(client)
    stolen = client.cookies.get(REFRESH_COOKIE)
    assert stolen
    legit = (await refresh_with(client, stolen)).cookies.get(REFRESH_COOKIE)
    assert legit
    clock.advance(seconds=30)  # well past the two-tab grace window

    replay = await refresh_with(other_client, stolen)
    victim = await refresh_with(client, legit)

    assert replay.status_code == 401
    assert replay.json()["code"] == "AUTH_REFRESH_REUSED"
    assert victim.status_code == 401  # the whole session is gone, for the thief and the owner
    assert victim.json()["code"] == "AUTH_REFRESH_INVALID"


async def test_two_tabs_refreshing_at_once_get_a_retryable_conflict(client: AsyncClient) -> None:
    await register(client)
    original = client.cookies.get(REFRESH_COOKIE)
    assert original
    newer = (await refresh_with(client, original)).cookies.get(REFRESH_COOKIE)
    assert newer

    race = await refresh_with(client, original)  # second tab, same instant
    retry = await refresh_with(client, newer)  # it retries with the cookie the browser now holds

    assert race.status_code == 409
    assert race.json()["code"] == "AUTH_REFRESH_RACE"
    assert retry.status_code == 200


async def test_sessions_slide_but_never_outlive_the_absolute_limit(mongo_uri: str) -> None:
    clock = FrozenClock()
    settings = build_settings(
        mongo_uri,
        auth=auth_settings(refresh_token_ttl_seconds=3600, refresh_absolute_ttl_seconds=7200),
    )
    async with running_client(build_app(settings, clock)) as http:
        await register(http)
        clock.advance(minutes=50)
        assert (await http.post("/v1/auth/refresh", headers=CSRF)).status_code == 200  # slides
        clock.advance(minutes=50)
        assert (await http.post("/v1/auth/refresh", headers=CSRF)).status_code == 200
        clock.advance(minutes=25)  # 125 min after sign-in: past the 2 h absolute limit
        expired = await http.post("/v1/auth/refresh", headers=CSRF)

    assert expired.status_code == 401
    assert expired.json()["code"] == "AUTH_REFRESH_INVALID"


async def test_idle_sessions_expire(mongo_uri: str) -> None:
    clock = FrozenClock()
    settings = build_settings(mongo_uri, auth=auth_settings(refresh_token_ttl_seconds=3600))
    async with running_client(build_app(settings, clock)) as http:
        await register(http)
        clock.advance(minutes=61)
        response = await http.post("/v1/auth/refresh", headers=CSRF)

    assert response.json()["code"] == "AUTH_REFRESH_INVALID"


async def test_refresh_fails_and_ends_the_session_for_disabled_accounts(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await register(client)
    await User.get_pymongo_collection().update_one(
        {"_id": PydanticObjectId(account.user_id)}, {"$set": {"disabled_at": clock.now()}}
    )

    response = await client.post("/v1/auth/refresh", headers=CSRF)

    assert response.json()["code"] == "AUTH_REFRESH_INVALID"
    session = await AuthSession.find_one({"user_id": PydanticObjectId(account.user_id)})
    assert session is not None
    assert session.revoked_reason == "account_inactive"


async def test_logout_ends_the_session_and_clears_the_cookie(client: AsyncClient) -> None:
    await register(client)
    cookie = client.cookies.get(REFRESH_COOKIE)
    assert cookie

    response = await client.post("/v1/auth/logout", headers=CSRF)

    assert response.status_code == 204
    [set_cookie] = response.headers.get_list("set-cookie")
    assert set_cookie.startswith(f'{REFRESH_COOKIE}=""') or "Max-Age=0" in set_cookie
    assert (await refresh_with(client, cookie)).json()["code"] == "AUTH_REFRESH_INVALID"


async def test_logout_needs_csrf_and_is_idempotent(other_client: AsyncClient) -> None:
    missing_header = await other_client.post("/v1/auth/logout")
    no_session = await other_client.post("/v1/auth/logout", headers=CSRF)

    assert missing_header.status_code == 403
    assert no_session.status_code == 204


async def test_logout_all_signs_out_every_device_and_access_token(
    client: AsyncClient, other_client: AsyncClient
) -> None:
    account = await register(client)
    second_device = await login(other_client, account.email)
    assert second_device.status_code == 200

    response = await client.post("/v1/auth/logout-all", headers=account.headers)

    assert response.status_code == 204
    assert (await other_client.post("/v1/auth/refresh", headers=CSRF)).status_code == 401
    assert (await client.post("/v1/auth/refresh", headers=CSRF)).status_code == 401
    stale = await client.get("/v1/me", headers=account.headers)
    assert stale.json()["code"] == "AUTH_SESSION_REVOKED"
