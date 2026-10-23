"""``/v1/admin/analytics``: sign-ups, active users, assistant answers and errors by code, and
how full the database is, over a database of its own so every count is known."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from beanie import PydanticObjectId
from httpx import AsyncClient

from app.platform.auth.models import AuthSession
from tests.helpers import FrozenClock, build_app, build_settings, login, register, running_client
from tests.platform.admin.conftest import admin_account

USAGE = "/v1/admin/analytics/usage"


@pytest.fixture
async def own_client(own_mongo_uri: str, clock: FrozenClock) -> AsyncIterator[AsyncClient]:
    async with running_client(build_app(build_settings(own_mongo_uri), clock)) as client:
        yield client


async def usage_once_counted(
    client: AsyncClient, headers: dict[str, str], code: str
) -> dict[str, Any]:
    """The usage, once the error just answered is counted (that is written in the background)."""
    for _ in range(50):
        found = (await client.get(USAGE, headers=headers)).json()
        if code in [item["code"] for item in found["errors"]]:
            return dict(found)
        await asyncio.sleep(0.02)
    raise AssertionError(f"{code} never counted")


async def test_counts_people_answers_and_errors(
    own_client: AsyncClient, clock: FrozenClock
) -> None:
    client = own_client
    admin = await admin_account(client)
    asha = await register(client)
    await register(client)
    assert (await login(client, asha.email, "wrong-password-1")).status_code == 401
    run = await client.post(
        "/v1/agents/assistant/runs", headers=asha.headers, json={"input": "pin habits"}
    )
    assert run.status_code == 201

    usage = await usage_once_counted(client, admin.headers, "AUTH_INVALID_CREDENTIALS")
    today = clock.now().date().isoformat()
    assert (usage["total_users"], usage["active_users"]) == (3, 3)
    assert usage["signups"] == [{"date": today, "count": 3}]
    assert usage["ai"]["runs"] == 1
    answered = run.json()
    static, fell_back = answered["mode"] == "static", answered["reason"] is not None
    assert (usage["ai"]["by_rules"], usage["ai"]["fell_back"]) == (
        int(static),
        int(static and fell_back),
    )
    assert {"code": "AUTH_INVALID_CREDENTIALS", "count": 1} in usage["errors"]
    assert (usage["start"], usage["end"]) <= (today, today)


async def test_a_span_with_nobody_counts_nothing(own_client: AsyncClient) -> None:
    admin = await admin_account(own_client)
    usage = (
        await own_client.get(
            USAGE, headers=admin.headers, params={"from": "2001-01-01", "to": "2001-01-31"}
        )
    ).json()
    assert (usage["active_users"], usage["signups"], usage["ai"]["runs"]) == (0, [], 0)
    assert usage["ai"]["fallback_rate_pct"] is None


async def test_someone_back_since_still_counts_for_the_days_they_came(
    own_client: AsyncClient, clock: FrozenClock
) -> None:
    """Counted from their sessions, not their last sign-in: coming back today doesn't move them
    out of last week."""
    admin = await admin_account(own_client)
    asha = await register(own_client)
    week_ago = clock.now() - timedelta(days=7)
    await AuthSession.get_pymongo_collection().update_many(
        {"user_id": PydanticObjectId(asha.user_id)},
        {"$set": {"created_at": week_ago, "last_used_at": week_ago}},
    )
    await login(own_client, asha.email)  # and back today

    day = week_ago.date().isoformat()
    usage = (
        await own_client.get(USAGE, headers=admin.headers, params={"from": day, "to": day})
    ).json()
    assert usage["active_users"] == 1


@pytest.mark.parametrize(
    ("span", "reason"),
    [
        ({"from": "2026-10-10", "to": "2026-10-01"}, "from_after_to"),
        ({"from": "2020-01-01", "to": "2026-01-01"}, "too_long"),
        ({"from": "0001-01-01", "to": "0001-01-02"}, "out_of_range"),
        ({"from": "9999-12-30", "to": "9999-12-31"}, "out_of_range"),
    ],
)
async def test_a_range_it_cannot_count_is_refused(
    own_client: AsyncClient, span: dict[str, str], reason: str
) -> None:
    admin = await admin_account(own_client)
    refused = await own_client.get(USAGE, headers=admin.headers, params=span)
    assert (refused.status_code, refused.json()["code"]) == (422, "RANGE_INVALID")
    assert refused.json()["details"]["reason"] == reason


async def test_says_how_full_the_database_is(own_client: AsyncClient) -> None:
    admin = await admin_account(own_client)
    found = (await own_client.get("/v1/admin/analytics/db", headers=admin.headers)).json()
    assert found["quota_mb"] == 512
    assert 0 <= found["used_pct"] < 100
    users = next(item for item in found["collections"] if item["name"] == "users")
    assert users["count"] == 1
