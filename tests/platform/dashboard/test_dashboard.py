"""``/v1/dashboard`` and ``/v1/admin/dashboard``: widgets from every package, each user's own
figures, an honest state for a widget that fails, and aggregates only for admins."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient

from app.core.config import DashboardSettings, ModulesSettings
from app.platform.dashboard.service import discover_widgets
from app.platform.plugins import PluginError, subpackages
from tests.helpers import (
    TEST_MODULE_PACKAGES,
    Account,
    FrozenClock,
    build_app,
    build_settings,
    make_admin,
    register,
    running_client,
)

MakePackage = Callable[[dict[str, dict[str, str]]], str]


async def widgets(
    client: AsyncClient, headers: dict[str, str], query: str = "", path: str = "/v1/dashboard"
) -> dict[str, Any]:
    response = await client.get(f"{path}{query}", headers=headers)
    assert response.status_code == 200, response.text
    return {item["key"]: item for item in response.json()["widgets"]}


async def add_notes(client: AsyncClient, account: Account, *texts: str) -> None:
    for text in texts:
        created = await client.post("/v1/notes", headers=account.headers, json={"text": text})
        assert created.status_code == 201


def today(clock: FrozenClock) -> Any:
    return clock.now().astimezone(ZoneInfo("Asia/Kolkata")).date()


# ------------------------------------------------------------------ my dashboard


async def test_shows_only_my_own_figures(client: AsyncClient, other_client: AsyncClient) -> None:
    owner = await register(client)
    other = await register(other_client)
    await add_notes(client, owner, "a", "b")
    await add_notes(other_client, other, "c")

    mine = await widgets(client, owner.headers)
    theirs = await widgets(other_client, other.headers)

    assert mine["notes_created"]["data"] == {
        "kind": "kpi", "value": 2, "previous": 0, "format": "number", "currency": None
    }  # fmt: skip
    assert theirs["notes_created"]["data"]["value"] == 1
    assert {item["title"] for item in mine["notes_latest"]["data"]["items"]} == {"a", "b"}
    assert [item["title"] for item in theirs["notes_latest"]["data"]["items"]] == ["c"]
    points = mine["notes_per_day"]["data"]["points"]
    assert len(points) == 30  # the default period, with no gaps
    assert points[-1]["value"] == 2
    assert sum(point["value"] for point in points) == 2


async def test_every_package_contributes(client: AsyncClient) -> None:
    account = await register(client)

    mine = await widgets(client, account.headers)

    assert {(item["owner"], item["kind"]) for item in mine.values()} >= {
        ("activity", "kpi"), ("activity", "list"), ("agents", "series"), ("notes", "kpi")
    }  # fmt: skip
    assert all(point["value"] == 0 for point in mine["agents_requests"]["data"]["points"])


async def test_period_and_trend(client: AsyncClient, clock: FrozenClock) -> None:
    account = await register(client)
    await add_notes(client, account, "earlier")
    clock.advance(days=10)
    await add_notes(client, account, "recent", "today")
    end = today(clock)

    week = await widgets(client, account.headers, f"?from={end - timedelta(days=6)}&to={end}")
    one_day = await widgets(client, account.headers, f"?from={end}&to={end}")

    assert week["notes_created"]["data"]["value"] == 2
    assert week["notes_created"]["data"]["previous"] == 1  # the note from ten days ago
    assert len(week["notes_per_day"]["data"]["points"]) == 7
    assert [point["value"] for point in one_day["notes_per_day"]["data"]["points"]] == [2]


@pytest.mark.parametrize(
    ("query", "reason"),
    [("?from=2026-10-12&to=2026-10-01", "from_after_to"), ("?from=2020-01-01", "too_long")],
)
async def test_impossible_periods_are_rejected(
    client: AsyncClient, query: str, reason: str
) -> None:
    account = await register(client)

    response = await client.get(f"/v1/dashboard{query}", headers=account.headers)

    assert response.status_code == 422
    assert response.json()["code"] == "RANGE_INVALID"
    assert response.json()["details"]["reason"] == reason


async def test_a_module_i_turned_off_takes_its_widgets_with_it(client: AsyncClient) -> None:
    account = await register(client)
    await client.patch(
        "/v1/modules/notes/preferences", headers=account.headers, json={"enabled": False}
    )

    mine = await widgets(client, account.headers)

    assert not any(key.startswith("notes_") for key in mine)
    assert "activity_actions" in mine


DEMO = {
    "manifest.py": "from app.platform import ModuleManifest\n"
    'MANIFEST = ModuleManifest(key="demo", version="1.0.0", status="live", order=1)\n',
    "widgets.py": """
import asyncio

from app.platform import Kpi, Series, widget


@widget("kpi")
async def demo_fine(ctx, period):
    return Kpi(value=7)


@widget("kpi")
async def demo_broken(ctx, period):
    raise RuntimeError("database exploded")


@widget("kpi")
async def demo_slow(ctx, period):
    await asyncio.sleep(5)
    return Kpi(value=1)


@widget("series")
async def demo_wrong_shape(ctx, period):
    return Kpi(value=1)
""",
}


async def test_a_failing_widget_is_reported_not_fatal(
    plugin_package: MakePackage, mongo_uri: str
) -> None:
    package = plugin_package({"demo": DEMO})
    settings = build_settings(
        mongo_uri,
        modules=ModulesSettings(_env_file=None, packages=(*TEST_MODULE_PACKAGES, package)),
        dashboard=DashboardSettings(_env_file=None, widget_timeout_seconds=0.5),
    )

    async with running_client(build_app(settings)) as http:
        account = await register(http)
        mine = await widgets(http, account.headers)

    assert mine["demo_fine"]["data"]["value"] == 7
    assert mine["demo_broken"]["data"] is None
    assert mine["demo_slow"]["data"] is None
    assert mine["demo_wrong_shape"]["data"] is None
    assert mine["notes_created"]["data"] is not None  # everything else still loads


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("from app.platform import widget\n@widget('kpi')\nasync def count(ctx, period): ...\n",
         "must start with 'demo_'"),
        ("from app.platform import widget\n@widget('kpi')\ndef demo_sync(ctx, period): ...\n",
         "must be async"),
    ],
)  # fmt: skip
def test_malformed_widgets_stop_startup(
    plugin_package: MakePackage, source: str, message: str
) -> None:
    package = plugin_package({"demo": {"widgets.py": source}})

    with pytest.raises(PluginError, match=message):
        discover_widgets(subpackages(package))


# ------------------------------------------------------------------ all users (admin)


async def test_admin_sees_aggregates_across_users_but_no_lists(
    client: AsyncClient, other_client: AsyncClient
) -> None:
    admin = {"Authorization": f"Bearer {await make_admin(client)}"}
    before = await widgets(client, admin, path="/v1/admin/dashboard")
    first = await register(other_client, name="First")
    await add_notes(other_client, first, "one", "two")
    second = await register(other_client, name="Second")
    await add_notes(other_client, second, "three")

    after = await widgets(client, admin, path="/v1/admin/dashboard")

    created = after["notes_created"]["data"]["value"] - before["notes_created"]["data"]["value"]
    assert created == 3  # both users' notes, counted together
    assert all(item["kind"] != "list" for item in after.values())  # no individual records
    assert "notes_latest" not in after


async def test_the_all_users_view_is_for_admins_only(client: AsyncClient) -> None:
    user = await register(client)

    response = await client.get("/v1/admin/dashboard", headers=user.headers)

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
