"""The lazy path: the bell's poll (``GET /v1/notifications``) runs what is due for the user,
only for modules available to them, and a slow or broken job never fails the poll."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from beanie import PydanticObjectId
from httpx import AsyncClient

from app.core.config import JobsSettings, ModulesSettings
from app.platform.users.models import User
from tests.fixtures.modules.notes.jobs import RUNS
from tests.helpers import (
    TEST_MODULE_PACKAGES,
    Account,
    FrozenClock,
    build_app,
    build_settings,
    json_log_lines,
    register,
    running_client,
)

MakePackage = Callable[[dict[str, dict[str, str]]], str]


async def poll(client: AsyncClient, account: Account) -> None:
    response = await client.get(
        "/v1/notifications?unread=true&page_size=1", headers=account.headers
    )
    assert response.status_code == 200, response.text


async def test_the_bell_runs_due_jobs_once_a_day(client: AsyncClient, clock: FrozenClock) -> None:
    account = await register(client)

    for _ in range(3):
        await poll(client, account)
    today = RUNS[account.user_id]
    clock.advance(days=1)
    await poll(client, account)

    assert today == 1
    assert RUNS[account.user_id] == 2


async def test_a_module_i_turned_off_runs_no_jobs_for_me(client: AsyncClient) -> None:
    account = await register(client)
    await client.patch(
        "/v1/modules/notes/preferences", headers=account.headers, json={"enabled": False}
    )

    await poll(client, account)

    assert RUNS[account.user_id] == 0


TROUBLE = {
    "manifest.py": "from app.platform import ModuleManifest\n"
    'MANIFEST = ModuleManifest(key="demo", version="1.0.0", status="live", order=1)\n',
    "jobs.py": """
import asyncio

from app.platform import job


@job()
async def demo_slow(ctx):
    await asyncio.sleep(5)
    return 1


@job("each")
async def demo_broken(ctx):
    raise RuntimeError("database exploded")
""",
}


async def test_a_slow_or_broken_job_never_fails_the_poll(
    plugin_package: MakePackage, mongo_uri: str, capsys: pytest.CaptureFixture[str]
) -> None:
    package = plugin_package({"demo": TROUBLE})
    settings = build_settings(
        mongo_uri,
        modules=ModulesSettings(_env_file=None, packages=(*TEST_MODULE_PACKAGES, package)),
        jobs=JobsSettings(_env_file=None, lazy_timeout_seconds=0.2),
    )

    async with running_client(build_app(settings)) as http:
        account = await register(http)
        capsys.readouterr()
        await poll(http, account)

    failures = [e for e in json_log_lines(capsys.readouterr().out) if e["event"] == "job_failed"]
    assert {(e["job"], e["error_type"]) for e in failures} == {
        ("demo_slow", "TimeoutError"),
        ("demo_broken", "RuntimeError"),
    }
    assert RUNS[account.user_id] == 1  # the other jobs still ran


async def test_a_job_that_cannot_even_start_never_fails_the_poll(client: AsyncClient) -> None:
    account = await register(client)
    await User.get_pymongo_collection().update_one(
        {"_id": PydanticObjectId(account.user_id)}, {"$set": {"timezone": "Mars/Olympus"}}
    )  # no local day, so no lease can be taken

    await poll(client, account)

    assert RUNS[account.user_id] == 0
