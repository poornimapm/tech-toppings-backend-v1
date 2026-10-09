from __future__ import annotations

from app.core.config import RateLimitSettings
from tests.helpers import (
    PASSWORD,
    build_app,
    build_settings,
    register,
    running_client,
    unique_email,
)


async def test_login_attempts_are_rate_limited_per_client(mongo_uri: str) -> None:
    settings = build_settings(
        mongo_uri, rate_limit=RateLimitSettings(_env_file=None, auth_login="2/minute")
    )
    async with running_client(build_app(settings)) as http:
        account = await register(http)
        payload = {"email": account.email, "password": PASSWORD}
        responses = [await http.post("/v1/auth/login", json=payload) for _ in range(3)]

    assert [r.status_code for r in responses] == [200, 200, 429]
    limited = responses[-1]
    assert limited.json()["code"] == "RATE_LIMITED"
    assert 0 < int(limited.headers["Retry-After"]) <= 60
    assert limited.json()["details"]["retry_after_seconds"] == int(limited.headers["Retry-After"])


async def test_each_endpoint_has_its_own_budget(mongo_uri: str) -> None:
    settings = build_settings(
        mongo_uri, rate_limit=RateLimitSettings(_env_file=None, auth_register="1/minute")
    )
    async with running_client(build_app(settings)) as http:
        first = await register(http)
        second = await http.post(
            "/v1/auth/register", json={"email": unique_email(), "name": "B", "password": PASSWORD}
        )
        login_still_fine = await http.post(
            "/v1/auth/login", json={"email": first.email, "password": PASSWORD}
        )

    assert second.status_code == 429
    assert login_still_fine.status_code == 200
