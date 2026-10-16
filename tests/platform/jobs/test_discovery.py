"""Jobs are found in each package's ``jobs.py``, like tools and widgets: names start with the
owner's key and are unique, handlers are async, and daily jobs run first."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI

from app.platform import job
from app.platform.jobs.runner import discover_jobs
from app.platform.plugins import PluginError, subpackages

MakePackage = Callable[[dict[str, dict[str, str]]], str]

EACH_THEN_DAILY = """
from app.platform import job


@job("each")
async def demo_each(ctx):
    return 0


@job()
async def demo_daily(ctx):
    return 0
"""


def test_found_under_their_owner_with_daily_jobs_first(plugin_package: MakePackage) -> None:
    package = plugin_package({"demo": {"jobs.py": EACH_THEN_DAILY}})

    jobs = discover_jobs([*subpackages(package), "tests.fixtures.modules.notes"])

    assert [(item.owner, item.spec.name, item.spec.cadence) for item in jobs] == [
        ("demo", "demo_daily", "daily"),
        ("notes", "notes_tick", "daily"),
        ("demo", "demo_each", "each"),
    ]


def test_the_app_knows_the_platform_and_module_jobs(app: FastAPI) -> None:
    jobs = {(item.owner, item.spec.name, item.spec.cadence) for item in app.state.jobs}

    assert {
        ("notes", "notes_tick", "daily"),
        ("expenses", "expenses_recurring", "daily"),
        ("reminders", "reminders_deliver", "each"),
    } <= jobs


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("from app.platform import job\n@job()\nasync def tick(ctx): ...\n",
         "must start with 'demo_'"),
        ("from app.platform import job\n@job()\ndef demo_sync(ctx): ...\n", "must be async"),
    ],
)  # fmt: skip
def test_malformed_jobs_stop_startup(
    plugin_package: MakePackage, source: str, message: str
) -> None:
    package = plugin_package({"demo": {"jobs.py": source}})

    with pytest.raises(PluginError, match=message):
        discover_jobs(subpackages(package))


def test_a_sync_handler_is_refused_by_the_decorator() -> None:
    def demo_sync(ctx: object) -> int:  # noqa: ARG001
        return 0

    with pytest.raises(TypeError, match="demo_sync must be async"):
        job()(demo_sync)  # type: ignore[type-var]


def test_two_packages_cannot_share_a_job_name(plugin_package: MakePackage) -> None:
    first = plugin_package({"demo": {"jobs.py": EACH_THEN_DAILY}})
    second = plugin_package({"demo": {"jobs.py": EACH_THEN_DAILY}})

    with pytest.raises(PluginError, match=r"duplicate names in jobs\.py: \['demo_daily'"):
        discover_jobs([*subpackages(first), *subpackages(second)])
