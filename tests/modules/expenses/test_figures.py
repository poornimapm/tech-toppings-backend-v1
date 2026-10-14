"""What the platform shows and does with expenses: the Welcome tile, dashboard widgets, the
assistant's tools, and that a switched-off module is off everywhere."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from httpx import AsyncClient

from app.core.money import to_major, to_minor
from tests.helpers import FrozenClock, make_admin, register
from tests.modules.expenses.conftest import ENTRIES, add
from tests.platform.agents.conftest import AgentClient, ask


def today_of(clock: FrozenClock) -> date:
    return clock.now().astimezone(ZoneInfo("Asia/Kolkata")).date()


async def widgets(client: AsyncClient, headers: dict[str, str], path: str = "/v1/dashboard") -> Any:
    response = await client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    return {item["key"]: item["data"] for item in response.json()["widgets"]}


def test_money_converts_exactly_between_units() -> None:
    assert (to_minor("250.50", "INR"), to_minor(19.99, "INR"), to_minor(0.005, "INR")) == (
        25050,
        1999,
        1,
    )
    assert (to_minor(1200, "JPY"), to_minor("1.2345", "KWD")) == (1200, 1235)
    assert (to_major(25050, "INR"), to_major(1200, "JPY"), to_major(1235, "KWD")) == (
        250.5,
        1200.0,
        1.235,
    )


async def test_the_tile_shows_what_was_spent_this_month(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await register(client)
    today = today_of(clock)
    last_month = today.replace(day=1) - timedelta(days=1)
    await add(client, account, amount_minor=12550, date=today.isoformat())
    await add(client, account, amount_minor=30000, date=today.replace(day=1).isoformat())
    await add(client, account, amount_minor=99900, date=last_month.isoformat())
    await add(
        client,
        account,
        kind="income",
        category_id="salary",
        amount_minor=5000000,
        date=today.isoformat(),
    )

    module = (await client.get("/v1/modules/expenses", headers=account.headers)).json()

    assert (module["status"], module["available"]) == ("beta", True)
    assert module["stat"] == {
        "value": 425.5,
        "format": "currency",
        "currency": "INR",
        "label": "month_total",
    }


async def test_the_dashboard_shows_my_money_and_admins_see_totals(
    client: AsyncClient, other_client: AsyncClient, clock: FrozenClock
) -> None:
    asha = await register(client)
    today = today_of(clock)
    await add(client, asha, amount_minor=20000, date=today.isoformat())
    await add(client, asha, amount_minor=5000, date=(today - timedelta(days=2)).isoformat())
    await add(client, asha, amount_minor=10000, date=(today - timedelta(days=40)).isoformat())
    await add(
        client,
        asha,
        kind="income",
        category_id="salary",
        amount_minor=900000,
        date=today.isoformat(),
    )

    mine = await widgets(client, asha.headers)
    ravi = await register(other_client)
    await add(other_client, ravi, amount_minor=1000, date=today.isoformat())
    theirs = await widgets(other_client, ravi.headers)
    admin = {"Authorization": f"Bearer {await make_admin(other_client)}"}
    everyone = await widgets(other_client, admin, "/v1/admin/dashboard")

    assert mine["expenses_spent"] == {
        "kind": "kpi",
        "value": 250.0,
        "previous": 100.0,
        "format": "currency",
        "currency": "INR",
    }
    assert (mine["expenses_received"]["value"], mine["expenses_received"]["previous"]) == (
        9000.0,
        0,
    )
    daily = mine["expenses_spent_daily"]
    assert (daily["format"], daily["currency"], len(daily["points"])) == ("currency", "INR", 30)
    assert [point["value"] for point in daily["points"][-3:]] == [50.0, 0, 200.0]
    assert theirs["expenses_spent"]["value"] == 10.0
    assert everyone["expenses_spent"]["value"] >= 260.0  # totals across users, no rows


async def test_a_switched_off_module_is_off_everywhere(agent_client: AgentClient) -> None:
    async with agent_client(mode="static") as client:
        account = await register(client)
        await add(client, account)
        opened = await ask(client, account, "show my expenses")

        await client.patch(
            "/v1/modules/expenses/preferences", headers=account.headers, json={"enabled": False}
        )
        entries = await client.get(ENTRIES, headers=account.headers)
        categories = await client.get("/v1/expenses/categories", headers=account.headers)
        dashboard = await widgets(client, account.headers)
        asked = await ask(client, account, "show my expenses")

    assert opened["skill"] == "track-expenses"
    assert opened["message_code"] == "track-expenses.ready.open"
    assert opened["client_actions"] == [{"tool": "expenses_open", "arguments": {}}]
    assert (entries.status_code, categories.status_code) == (403, 403)
    assert entries.json()["code"] == "MODULE_DISABLED"
    assert not any(key.startswith("expenses_") for key in dashboard)
    # Its own skill is gone too, so the platform's module skill offers to bring it back.
    assert (asked["skill"], asked["client_actions"]) == ("manage-modules", [])
    assert asked["proposals"][0]["summary_code"] == "manage-modules.change.show"
