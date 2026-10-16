"""``POST /v1/internal/jobs/{job_key}/run``: the jobs-cron workflow's way in. Only the shared
token opens it, it catches up recently active users, and it never shows up in the API schema
or the logs.

Each test has a database of its own: the cron reads across users, and other tests' accounts
must not count."""

from __future__ import annotations

from typing import Any

import pytest
from beanie import PydanticObjectId
from fastapi import FastAPI
from httpx import AsyncClient, Response
from pydantic import SecretStr

from app.core.config import JobsSettings, RateLimitSettings, Settings
from app.platform.users.models import User
from tests.fixtures.modules.notes.jobs import RUNS
from tests.helpers import (
    CSRF,
    FrozenClock,
    build_app,
    build_settings,
    json_log_lines,
    register,
    running_client,
)

TOKEN = "cron-" + "t" * 43
WRONG = "wrong-" + "t" * 42


def jobs_settings(**values: Any) -> JobsSettings:
    return JobsSettings(_env_file=None, token=SecretStr(TOKEN), **values)


@pytest.fixture
def settings(own_mongo_uri: str) -> Settings:
    return build_settings(own_mongo_uri, jobs=jobs_settings())


async def run(
    client: AsyncClient, job_key: str = "notes_tick", token: str | None = TOKEN
) -> Response:
    headers = {"X-Job-Token": token} if token is not None else {}
    return await client.post(f"/v1/internal/jobs/{job_key}/run", headers=headers)


async def test_runs_due_jobs_for_recently_active_users(client: AsyncClient) -> None:
    first, second = await register(client), await register(client)

    once = await run(client)
    again = await run(client)
    everything = await run(client, "all")

    assert once.status_code == 200, once.text
    assert once.json() == {
        "users": 2,
        "ran": 2,
        "skipped": 0,
        "failed": 0,
        "done": 2,
        "more": False,
    }
    assert (again.json()["ran"], again.json()["skipped"]) == (0, 2)  # done for today
    assert everything.status_code == 200
    assert everything.json()["users"] == 2
    assert RUNS[first.user_id] == RUNS[second.user_id] == 1


async def test_disabled_and_signed_out_users_are_left_out(
    client: AsyncClient, clock: FrozenClock
) -> None:
    active, disabled = await register(client), await register(client)
    await User.get_pymongo_collection().update_one(
        {"_id": PydanticObjectId(disabled.user_id)}, {"$set": {"disabled_at": clock.now()}}
    )
    signed_out = await register(client)
    await client.post("/v1/auth/logout", headers=CSRF)

    response = await run(client)

    assert response.json()["users"] == 1
    assert (RUNS[active.user_id], RUNS[disabled.user_id], RUNS[signed_out.user_id]) == (1, 0, 0)


async def test_one_run_takes_the_most_recently_active_users_first(
    own_mongo_uri: str, clock: FrozenClock
) -> None:
    settings = build_settings(own_mongo_uri, jobs=jobs_settings(max_users_per_run=1))

    async with running_client(build_app(settings, clock)) as http:
        earlier = await register(http)
        clock.advance(minutes=5)
        latest = await register(http)
        response = await run(http)

    assert (response.json()["users"], response.json()["more"]) == (1, True)
    assert (RUNS[latest.user_id], RUNS[earlier.user_id]) == (1, 0)


async def test_one_broken_account_never_stops_the_run(
    client: AsyncClient, clock: FrozenClock
) -> None:
    healthy = await register(client)
    clock.advance(minutes=5)
    bad_zone = await register(client)
    clock.advance(minutes=5)
    unreadable = await register(client)  # the most recent, so the run meets it first
    users = User.get_pymongo_collection()
    await users.update_one(
        {"_id": PydanticObjectId(bad_zone.user_id)}, {"$set": {"timezone": "Mars/Olympus"}}
    )
    await users.update_one(
        {"_id": PydanticObjectId(unreadable.user_id)}, {"$set": {"locale": "xx"}}
    )

    response = await run(client)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "users": 2,  # the unreadable account is left out
        "ran": 1,
        "skipped": 0,
        "failed": 1,  # the bad time zone's job
        "done": 1,
        "more": False,
    }
    assert RUNS[healthy.user_id] == 1


async def test_guesses_are_rate_limited_before_the_token_is_checked(own_mongo_uri: str) -> None:
    settings = build_settings(
        own_mongo_uri,
        jobs=jobs_settings(),
        rate_limit=RateLimitSettings(_env_file=None, jobs_run="2/minute"),
    )

    async with running_client(build_app(settings)) as http:
        responses = [await run(http, token=WRONG) for _ in range(3)]

    assert [r.status_code for r in responses] == [401, 401, 429]


@pytest.mark.parametrize("token", [None, "", WRONG, TOKEN[:-1]])
async def test_a_missing_or_wrong_token_is_refused(client: AsyncClient, token: str | None) -> None:
    account = await register(client)

    response = await run(client, token=token)
    with_bearer = await client.post(
        "/v1/internal/jobs/notes_tick/run", headers=account.headers | {"X-Job-Token": token or ""}
    )

    assert (response.status_code, response.json()["code"]) == (401, "UNAUTHENTICATED")
    assert with_bearer.status_code == 401  # a user's own sign-in is not the job token
    assert RUNS[account.user_id] == 0


async def test_without_a_configured_token_every_call_is_refused(own_mongo_uri: str) -> None:
    async with running_client(build_app(build_settings(own_mongo_uri))) as http:
        account = await register(http)
        response = await run(http)

    assert response.status_code == 401
    assert RUNS[account.user_id] == 0


async def test_an_unknown_job_is_not_found_only_once_authenticated(client: AsyncClient) -> None:
    unauthenticated = await run(client, "nobody_knows", token=WRONG)
    authenticated = await run(client, "nobody_knows")

    assert unauthenticated.status_code == 401
    assert (authenticated.status_code, authenticated.json()["details"]) == (
        404,
        {"job": "nobody_knows"},
    )


def test_the_route_is_left_out_of_the_api_schema(app: FastAPI) -> None:
    assert [path for path in app.openapi()["paths"] if "internal" in path] == []


async def test_the_token_never_reaches_the_logs(
    client: AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    await register(client)
    capsys.readouterr()

    await run(client)
    await run(client, token=WRONG)

    output = capsys.readouterr().out
    assert TOKEN not in output
    assert "wrong-" not in output
    (summary,) = [e for e in json_log_lines(output) if e["event"] == "jobs_cron_run"]
    assert (summary["users"], summary["ran"]) == (1, 1)
