"""The daily lease: a daily job runs once per user and local day however often, or however
concurrently, it is asked to; a failed run is retried once its lease ends. An ``each`` job
takes no lease and runs every time."""

from __future__ import annotations

import asyncio
import importlib
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.core.config import ModulesSettings, Settings
from app.platform.jobs.models import JobRun
from app.platform.jobs.runner import Leases, catch_up
from tests.fixtures.modules.notes.jobs import RUNS
from tests.helpers import (
    TEST_MODULE_PACKAGES,
    FrozenClock,
    actor_of,
    build_app,
    build_settings,
    register,
    running_client,
)

MakePackage = Callable[[dict[str, dict[str, str]]], str]
TODAY, TOMORROW = "2026-10-15", "2026-10-16"
WITHIN = 5.0


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 10, 15, 18, 0, tzinfo=UTC))  # 23:30 in Kolkata


@pytest.fixture
def leases(
    client: AsyncClient,  # noqa: ARG001 - requested so the database is ready
    settings: Settings,
    clock: FrozenClock,
) -> Leases:
    return Leases(clock, settings.jobs)


def job_key() -> str:
    return f"demo_{uuid.uuid4().hex[:8]}"


async def test_a_lease_is_held_until_its_run_finishes_or_its_time_ends(
    leases: Leases, settings: Settings, clock: FrozenClock
) -> None:
    key = job_key()

    first = await leases.claim(key, "user", TODAY)
    held = await leases.claim(key, "user", TODAY)
    clock.advance(seconds=settings.jobs.lease_seconds)  # the first run never finished
    retried = await leases.claim(key, "user", TODAY)
    await leases.finish(key, "user", TODAY)
    done = await leases.claim(key, "user", TODAY)
    next_day = await leases.claim(key, "user", TOMORROW)

    assert (first, held, retried, done, next_day) == (True, False, True, False, True)


async def test_only_one_of_many_concurrent_claims_wins(leases: Leases) -> None:
    key = job_key()

    claims = await asyncio.gather(*(leases.claim(key, "user", TODAY) for _ in range(10)))

    assert sorted(claims) == [False] * 9 + [True]


async def test_runs_once_per_local_day(
    app: FastAPI, client: AsyncClient, clock: FrozenClock
) -> None:
    account = await register(client)
    ctx = await actor_of(app, account)

    first = await catch_up(ctx, within=WITHIN, only="notes_tick")
    again = await catch_up(ctx, within=WITHIN, only="notes_tick")
    clock.advance(days=1)
    next_day = await catch_up(ctx, within=WITHIN, only="notes_tick")

    assert RUNS[account.user_id] == 2
    assert (first.ran, first.done, again.skipped, next_day.ran) == (1, 1, 1, 1)


async def test_concurrent_catch_ups_run_a_daily_job_once(app: FastAPI, client: AsyncClient) -> None:
    account = await register(client)
    ctx = await actor_of(app, account)

    results = await asyncio.gather(
        *(catch_up(ctx, within=WITHIN, only="notes_tick") for _ in range(2))
    )

    assert RUNS[account.user_id] == 1
    assert sorted((item.ran, item.skipped) for item in results) == [(0, 1), (1, 0)]


async def test_the_day_turns_at_the_users_own_midnight(
    app: FastAPI, client: AsyncClient, clock: FrozenClock
) -> None:
    kolkata = await actor_of(app, await register(client))
    other = await actor_of(app, await register(client))
    new_york = replace(other, user=other.user.model_copy(update={"timezone": "America/New_York"}))

    for ctx in (kolkata, new_york):
        await catch_up(ctx, within=WITHIN, only="notes_tick")
    clock.advance(hours=1)  # 00:30 in Kolkata, 15:00 in New York: still the same UTC day
    for ctx in (kolkata, new_york):
        await catch_up(ctx, within=WITHIN, only="notes_tick")

    assert RUNS[str(kolkata.user.id)] == 2
    assert RUNS[str(new_york.user.id)] == 1


DEMO_MANIFEST = (
    "from app.platform import ModuleManifest\n"
    'MANIFEST = ModuleManifest(key="demo", version="1.0.0", status="live", order=1)\n'
)


def app_with(package: str, mongo_uri: str, clock: FrozenClock) -> FastAPI:
    """The test app plus a throwaway plugin package's modules."""
    modules = ModulesSettings(_env_file=None, packages=(*TEST_MODULE_PACKAGES, package))
    return build_app(build_settings(mongo_uri, modules=modules), clock)


FLAKY = {
    "manifest.py": DEMO_MANIFEST,
    "jobs.py": """
from app.platform import job

FAILING = True


@job()
async def demo_flaky(ctx):
    if FAILING:
        raise RuntimeError("upstream down")
    return 3
""",
}


async def test_a_failed_run_is_retried_once_its_lease_ends(
    plugin_package: MakePackage,
    mongo_uri: str,
    clock: FrozenClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = plugin_package({"demo": FLAKY})
    app = app_with(package, mongo_uri, clock)

    async with running_client(app) as http:
        ctx = await actor_of(app, await register(http))
        failed = await catch_up(ctx, within=WITHIN, only="demo_flaky")
        backing_off = await catch_up(ctx, within=WITHIN, only="demo_flaky")
        clock.advance(seconds=ctx.settings.jobs.lease_seconds)
        monkeypatch.setattr(importlib.import_module(f"{package}.demo.jobs"), "FAILING", False)
        retried = await catch_up(ctx, within=WITHIN, only="demo_flaky")
        done = await catch_up(ctx, within=WITHIN, only="demo_flaky")

    assert (failed.failed, backing_off.skipped, done.skipped) == (1, 1, 1)
    assert (retried.ran, retried.done) == (1, 3)


EVERY_TIME = {
    "manifest.py": DEMO_MANIFEST,
    "jobs.py": """
from app.platform import job

CALLS = []


@job("each")
async def demo_each(ctx):
    CALLS.append(ctx.user.id)
    return 1
""",
}


async def test_an_each_job_runs_on_every_catch_up_and_takes_no_lease(
    plugin_package: MakePackage, mongo_uri: str, clock: FrozenClock
) -> None:
    package = plugin_package({"demo": EVERY_TIME})
    app = app_with(package, mongo_uri, clock)

    async with running_client(app) as http:
        ctx = await actor_of(app, await register(http))
        results = [await catch_up(ctx, within=WITHIN, only="demo_each") for _ in range(3)]
        leases = await JobRun.get_pymongo_collection().count_documents(
            {"job_key": "demo_each", "scope_key": str(ctx.user.id)}
        )

    assert [(item.ran, item.skipped) for item in results] == [(1, 0)] * 3
    assert len(importlib.import_module(f"{package}.demo.jobs").CALLS) == 3
    assert leases == 0
