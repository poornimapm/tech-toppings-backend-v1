"""``/v1/expenses/budgets`` (U7): budgets for all spending, a category, or a tag over dates (a
trip). Their figures are the entries list's own, and each warns once when nearly spent and once
when over, in each window and for each limit."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient

from app.modules.expenses.planning import planning
from app.platform.notifications.inbox import Notification
from tests.helpers import Account, FrozenClock, register
from tests.modules.expenses.conftest import (
    CATEGORIES,
    ENTRIES,
    add,
    save,
    signed_up_at,
    travel,
)

BUDGETS = "/v1/expenses/budgets"
# Thursday 8 October in Kolkata, years ahead so the notifications counted outlive the test (TTL).
THURSDAY = datetime(2037, 10, 8, 6, tzinfo=UTC)
TODAY = "2037-10-08"


async def signed_up(client: AsyncClient, clock: FrozenClock) -> Account:
    return await signed_up_at(client, clock, THURSDAY)


async def spend(client: AsyncClient, account: Account, **changes: Any) -> dict[str, Any]:
    """An expense, dated today unless said otherwise."""
    return await add(client, account, **({"date": TODAY} | changes))


def budget(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "October",
        "scope": "overall",
        "period": "month",
        "limit_minor": 100000,
    }
    return body | changes


async def create(client: AsyncClient, account: Account, **changes: Any) -> dict[str, Any]:
    response = await client.post(BUDGETS, headers=account.headers, json=budget(**changes))
    assert response.status_code == 201, response.text
    return dict(response.json())


async def change(
    client: AsyncClient, account: Account, budget_id: str, **changes: Any
) -> dict[str, Any]:
    response = await client.patch(f"{BUDGETS}/{budget_id}", headers=account.headers, json=changes)
    assert response.status_code == 200, response.text
    return dict(response.json())


async def statuses(client: AsyncClient, account: Account) -> dict[str, dict[str, Any]]:
    """Each budget's status today, by name."""
    response = await client.get(f"{BUDGETS}?page_size=100", headers=account.headers)
    assert response.status_code == 200, response.text
    return {item["name"]: item["status"] for item in response.json()["items"]}


async def listed_spent(client: AsyncClient, account: Account, query: str) -> int:
    """What the entries list totals as spent with these filters."""
    response = await client.get(f"{ENTRIES}/summary?{query}", headers=account.headers)
    assert response.status_code == 200, response.text
    return sum(total["expense_minor"] for total in response.json()["totals"])


async def alerts(client: AsyncClient, account: Account) -> list[tuple[str, dict[str, Any]]]:
    """Budget notifications, newest first: (level, params)."""
    page = (await client.get("/v1/notifications?page_size=100", headers=account.headers)).json()
    return [
        (item["type"].removeprefix("expenses.budget_"), item["params"])
        for item in page["items"]
        if item["type"].startswith("expenses.budget_")
    ]


async def levels(client: AsyncClient, account: Account) -> list[str]:
    return [level for level, _ in await alerts(client, account)]


async def test_a_budget_counts_what_the_entries_list_totals(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await signed_up(client, clock)
    await spend(client, account, title="Rice", amount_minor=12500, date="2037-10-08")
    await spend(client, account, title="Bus", amount_minor=60000, category_id="transport")
    goa = {"date": "2037-10-03", "tags": ["Goa"]}
    await spend(client, account, title="Hotel", amount_minor=400000, category_id="travel", **goa)
    goa = {"date": "2037-10-06", "tags": ["goa", "friends"]}
    await spend(client, account, title="Dinner", amount_minor=150000, category_id="food", **goa)
    goa = {"date": "2037-10-07", "tags": ["goa"]}  # the day after the trip
    await spend(client, account, title="Cab", amount_minor=90000, category_id="transport", **goa)
    await spend(client, account, title="Rice", amount_minor=99900, date="2037-09-20")
    await spend(client, account, kind="income", category_id="salary", amount_minor=5000000)
    gone = await spend(client, account, title="Mistake", amount_minor=70000)
    await client.delete(f"{ENTRIES}/{gone['id']}", headers=account.headers)

    await create(client, account, name="October", limit_minor=1000000)
    groceries = {"scope": "category", "category_id": "groceries"}
    await create(client, account, name="Groceries", limit_minor=50000, **groceries)
    days = {"period": "custom", "start": "2037-10-02", "end": "2037-10-06", "limit_minor": 1000000}
    trip = await create(client, account, name="Goa trip", scope="tag", tag=" Goa ", **days)
    found = await statuses(client, account)

    month = "from=2037-10-01&to=2037-10-31"
    assert found["October"]["spent_minor"] == await listed_spent(client, account, month)
    assert found["October"]["spent_minor"] == 712500
    assert found["Groceries"]["spent_minor"] == await listed_spent(
        client, account, f"{month}&category_id=groceries"
    )
    assert found["Groceries"]["spent_minor"] == 12500
    assert trip["tag"] == "goa"
    assert found["Goa trip"]["spent_minor"] == await listed_spent(
        client, account, "from=2037-10-02&to=2037-10-06&tag=goa"
    )
    assert found["Goa trip"]["spent_minor"] == 550000
    # 8 days of 31 have passed (today included); 24 are left, today included.
    assert found["October"] == {
        "start": "2037-10-01",
        "end": "2037-10-31",
        "spent_minor": 712500,
        "left_minor": 287500,
        "pct": 71,
        "state": "ok",
        "days_left": 24,
        "per_day_left_minor": 11979,
        "pace_minor": 258064,
    }
    assert found["Goa trip"] == {
        "start": "2037-10-02",
        "end": "2037-10-06",
        "spent_minor": 550000,
        "left_minor": 450000,
        "pct": 55,
        "state": "ended",
        "days_left": 0,
        "per_day_left_minor": 0,
        "pace_minor": 1000000,
    }


async def test_windows_turn_at_the_users_own_midnight(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await signed_up(client, clock)
    await create(client, account, name="Week", period="week", limit_minor=10000)
    await create(client, account, name="Month", limit_minor=10000)
    days = {"period": "custom", "start": "2037-11-01", "end": "2037-11-08"}
    await create(client, account, name="Diwali", limit_minor=10000, **days)
    await spend(client, account, amount_minor=500, date="2037-10-11")  # a Sunday

    before = await statuses(client, account)
    clock.advance(days=3, hours=12, minutes=45)  # Monday 00:15 in Kolkata, Sunday in UTC
    monday = await statuses(client, account)
    clock.advance(days=20)  # 1 November 00:15 in Kolkata, still 31 October in UTC
    november = await statuses(client, account)

    assert (before["Week"]["start"], before["Week"]["end"]) == ("2037-10-05", "2037-10-11")
    assert before["Week"]["spent_minor"] == 500
    assert (monday["Week"]["start"], monday["Week"]["end"]) == ("2037-10-12", "2037-10-18")
    assert monday["Week"]["spent_minor"] == 0
    assert (november["Month"]["start"], november["Month"]["end"]) == ("2037-11-01", "2037-11-30")
    assert (november["Week"]["start"], november["Week"]["end"]) == ("2037-10-26", "2037-11-01")
    assert (before["Diwali"]["state"], before["Diwali"]["days_left"]) == ("upcoming", 8)
    assert before["Diwali"]["pace_minor"] == 0
    assert (november["Diwali"]["state"], november["Diwali"]["days_left"]) == ("ok", 8)
    assert november["Diwali"]["pace_minor"] == 1250  # the first of 8 days


async def test_near_and_over_each_warn_once_however_often_entries_are_saved(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await signed_up(client, clock)
    october = await create(client, account, limit_minor=1000)
    await spend(client, account, amount_minor=500)
    quiet = await levels(client, account)

    await spend(client, account, amount_minor=300)  # 80%: near
    tea = await spend(client, account, title="Tea", amount_minor=50)
    assert (await save(client, account, tea, amount_minor=60)).status_code == 200
    near = await alerts(client, account)
    await spend(client, account, amount_minor=200)  # past the limit: over
    biscuits = await spend(client, account, title="Biscuits", amount_minor=40)
    assert (await save(client, account, biscuits, title="Cookies")).status_code == 200
    await spend(client, account, amount_minor=10)

    assert quiet == []
    assert near == [
        (
            "near",
            {
                "module": "expenses",
                "budget_id": october["id"],
                "name": "October",
                "spent_minor": 800,
                "limit_minor": 1000,
                "currency": "INR",
                "pct": 80,
            },
        )
    ]
    assert await levels(client, account) == ["over", "near"]


async def test_only_the_highest_level_reached_is_told(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await signed_up(client, clock)
    await create(client, account, limit_minor=1000)
    groceries = {"scope": "category", "category_id": "groceries"}
    await create(
        client, account, name="Groceries", limit_minor=1250, alert_threshold=0, **groceries
    )

    await spend(client, account, amount_minor=1200)  # October is over; Groceries at 96% stays quiet

    assert [(level, params["name"]) for level, params in await alerts(client, account)] == [
        ("over", "October")
    ]


async def test_a_trip_budget_warns_once_for_its_own_tag_and_days(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await signed_up(client, clock)
    days = {"period": "custom", "start": "2037-10-06", "end": "2037-10-10"}
    trip = await create(
        client, account, name="Goa trip", scope="tag", tag="goa", limit_minor=10000, **days
    )
    await spend(client, account, amount_minor=9000)  # not tagged
    await spend(client, account, amount_minor=9000, tags=["goa"], date="2037-10-05")  # before
    quiet = await levels(client, account)

    await spend(client, account, title="Hotel", amount_minor=8000, tags=["Goa"], date="2037-10-07")
    await spend(client, account, title="Tea", amount_minor=100, tags=["goa"])

    assert quiet == []
    assert [
        (level, params["budget_id"], params["spent_minor"])
        for level, params in await alerts(client, account)
    ] == [("near", trip["id"], 8000)]


async def test_a_category_budget_warns_only_for_its_own_category(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await signed_up(client, clock)
    groceries = {"scope": "category", "category_id": "groceries"}
    made = await create(client, account, name="Groceries", limit_minor=1000, **groceries)
    await spend(client, account, amount_minor=900, category_id="transport")
    quiet = await levels(client, account)

    await spend(client, account, amount_minor=850)  # groceries, 85%

    assert quiet == []
    assert [(level, params["budget_id"]) for level, params in await alerts(client, account)] == [
        ("near", made["id"])
    ]


async def test_an_edit_on_its_own_can_bring_a_budget_near(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await signed_up(client, clock)
    await create(client, account, limit_minor=1000)
    rice = await spend(client, account, amount_minor=500)
    before = await levels(client, account)

    assert (await save(client, account, rice, amount_minor=800)).status_code == 200

    assert (before, await levels(client, account)) == ([], ["near"])


async def test_a_warning_is_not_told_again_once_its_notification_is_purged(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await signed_up(client, clock)
    made = await create(client, account, limit_minor=1000)
    await spend(client, account, amount_minor=800)  # 80%: near
    told = await levels(client, account)
    # What the TTL does after NOTIFICATIONS_RETENTION_DAYS, which can be shorter than a window.
    await Notification.get_pymongo_collection().delete_many({"params.budget_id": made["id"]})

    await spend(client, account, amount_minor=10)  # still near
    after_purge = await levels(client, account)
    await spend(client, account, amount_minor=200)  # over

    assert (told, after_purge) == (["near"], [])
    assert await levels(client, account) == ["over"]


async def test_a_trip_that_has_ended_or_not_started_stays_quiet(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await signed_up(client, clock)
    goa = {"tags": ["goa"]}
    hotel = await spend(client, account, title="Hotel", amount_minor=1200, date="2037-10-03", **goa)
    trip = {"scope": "tag", "tag": "goa", "period": "custom", "limit_minor": 1000}
    ended = await create(client, account, name="Goa", start="2037-10-02", end="2037-10-06", **trip)
    later = await create(
        client, account, name="Goa again", start="2037-11-01", end="2037-11-08", **trip
    )
    await spend(client, account, title="Train", amount_minor=1500, date="2037-11-02", **goa)
    assert (await save(client, account, hotel, title="Beach hotel")).status_code == 200

    assert (ended["status"]["state"], later["status"]["state"]) == ("ended", "upcoming")
    assert await levels(client, account) == []


async def test_a_budget_counts_in_its_own_currency(client: AsyncClient, clock: FrozenClock) -> None:
    account = await signed_up(client, clock)
    await spend(client, account, amount_minor=500)
    await create(client, account, name="Rupees", limit_minor=1000)
    moved = await client.patch("/v1/me", headers=account.headers, json={"base_currency": "USD"})
    await spend(client, account, amount_minor=900)  # in dollars now
    made = await create(client, account, name="Dollars", limit_minor=1000)  # 90%: near
    found = await statuses(client, account)

    assert moved.status_code == 200
    assert (found["Rupees"]["spent_minor"], found["Dollars"]["spent_minor"]) == (500, 900)
    assert made["currency"] == "USD"
    assert [
        (level, params["name"], params["currency"])
        for level, params in await alerts(client, account)
    ] == [("near", "Dollars", "USD")]


async def test_a_user_keeps_a_limited_number_of_budgets(
    client: AsyncClient, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(planning().budgets, "max_per_user", 2)
    account = await signed_up(client, clock)
    first = await create(client, account, name="One")
    await create(client, account, name="Two")

    refused = await client.post(BUDGETS, headers=account.headers, json=budget(name="Three"))
    await client.delete(f"{BUDGETS}/{first['id']}", headers=account.headers)
    await create(client, account, name="Three")  # deleting one makes room

    assert refused.status_code == 409
    assert (refused.json()["code"], refused.json()["details"]) == (
        "EXPENSE_BUDGET_LIMIT",
        {"max": 2},
    )


async def test_warnings_come_again_in_the_next_window_and_for_a_new_limit(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await signed_up(client, clock)
    week = await create(client, account, name="Week", period="week", limit_minor=1000)
    await spend(client, account, amount_minor=900)
    await change(client, account, week["id"], limit_minor=950)  # still near, at a new limit
    await change(client, account, week["id"], name="This week")  # same limit: nothing new
    await change(client, account, week["id"], limit_minor=900)  # now over
    told = await levels(client, account)

    clock.advance(days=7)
    await spend(client, account, amount_minor=850, date="2037-10-15")

    assert told == ["over", "near", "near"]
    assert await levels(client, account) == ["near", "over", "near", "near"]


async def test_a_new_budget_already_spent_warns_at_once_and_entries_brought_back_count(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await signed_up(client, clock)
    first = await spend(client, account, amount_minor=800)
    await client.delete(f"{ENTRIES}/{first['id']}", headers=account.headers)
    second = await spend(client, account, title="Oil", amount_minor=900)
    edited = await save(client, account, second, amount_minor=50)
    assert edited.status_code == 200

    await create(client, account, name="Gone", limit_minor=1000)  # 50 spent
    quiet = await levels(client, account)
    await client.post(f"{ENTRIES}/{first['id']}/undelete", headers=account.headers)  # 850
    near = await levels(client, account)
    await client.post(
        f"{ENTRIES}/{second['id']}/restore", headers=account.headers, json={"revision_no": 1}
    )  # Oil is 900 again: 1700
    await create(client, account, name="Small", limit_minor=1500)

    assert (quiet, near) == ([], ["near"])
    assert [(level, params["name"]) for level, params in await alerts(client, account)] == [
        ("over", "Small"),
        ("over", "Gone"),
        ("near", "Gone"),
    ]


async def test_other_peoples_entries_never_count(
    client: AsyncClient, other_client: AsyncClient, clock: FrozenClock
) -> None:
    asha, ravi = await register(client), await register(other_client)
    travel(clock, THURSDAY)
    await create(client, asha, name="Goa trip", scope="tag", tag="goa", limit_minor=1000)

    await spend(other_client, ravi, amount_minor=5000, tags=["goa"])

    assert (await statuses(client, asha))["Goa trip"]["spent_minor"] == 0
    assert await levels(client, asha) == []
    assert (await statuses(other_client, ravi)) == {}


async def test_a_budget_is_added_changed_and_deleted(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await signed_up(client, clock)
    made = await create(
        client, account, name="  Groceries ", scope="category", category_id="groceries"
    )

    days = {"period": "custom", "start": "2037-11-01", "end": "2037-11-08"}
    changed = await change(
        client, account, made["id"], scope="tag", category_id=None, tag="Diwali", **days
    )
    deleted = await client.delete(f"{BUDGETS}/{made['id']}", headers=account.headers)
    again = await client.patch(
        f"{BUDGETS}/{made['id']}", headers=account.headers, json={"limit_minor": 1}
    )

    assert (made["name"], made["currency"], made["alert_threshold"]) == ("Groceries", "INR", 0.8)
    assert (made["status"]["start"], made["status"]["state"]) == ("2037-10-01", "ok")
    assert (changed["scope"], changed["category_id"], changed["tag"]) == ("tag", None, "diwali")
    assert (changed["start"], changed["end"], changed["status"]["state"]) == (
        "2037-11-01",
        "2037-11-08",
        "upcoming",
    )
    assert deleted.status_code == 204
    assert again.status_code == 404
    assert await statuses(client, account) == {}


@pytest.mark.parametrize(
    "changes",
    [
        {"scope": "category"},  # which category?
        {"scope": "tag"},
        {"tag": "goa"},  # an overall budget counts everything
        {"scope": "category", "category_id": "groceries", "tag": "goa"},
        {"period": "custom", "start": "2037-10-01"},
        {"start": "2037-10-01", "end": "2037-10-31"},  # a month has its own days
        {"period": "custom", "start": "2037-10-09", "end": "2037-10-08"},
        {"limit_minor": 0},
        {"name": "   "},
        {"alert_threshold": 1.5},
        {"owner": "someone else"},
    ],
)
async def test_a_budget_must_fit_together(
    client: AsyncClient, changes: dict[str, Any], clock: FrozenClock
) -> None:
    account = await signed_up(client, clock)

    response = await client.post(BUDGETS, headers=account.headers, json=budget(**changes))

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_a_change_must_leave_a_budget_that_fits_and_a_usable_category(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await signed_up(client, clock)
    made = await create(client, account)
    pets = await client.post(
        CATEGORIES, headers=account.headers, json={"name": "Pets", "icon": "🐶", "accent": "teal"}
    )
    await client.delete(f"{CATEGORIES}/{pets.json()['id']}", headers=account.headers)

    async def refused(**changes: Any) -> str:
        response = await client.patch(
            f"{BUDGETS}/{made['id']}", headers=account.headers, json=changes
        )
        assert response.status_code == 422, response.text
        return str(response.json()["code"])

    unfitting = await refused(scope="tag")
    cleared = await refused(limit_minor=None)
    income = await refused(scope="category", category_id="salary")
    archived = await refused(scope="category", category_id=pets.json()["id"])
    added = await client.post(
        BUDGETS, headers=account.headers, json=budget(scope="category", category_id="nope")
    )

    assert (unfitting, cleared) == ("VALIDATION_ERROR", "VALIDATION_ERROR")
    assert (income, archived) == ("EXPENSE_CATEGORY_INVALID", "EXPENSE_CATEGORY_INVALID")
    assert added.json()["code"] == "EXPENSE_CATEGORY_INVALID"
    assert (await statuses(client, account))["October"]["spent_minor"] == 0  # unchanged


async def test_budgets_are_off_with_the_module(client: AsyncClient, clock: FrozenClock) -> None:
    account = await signed_up(client, clock)
    made = await create(client, account)
    anonymous = await client.get(BUDGETS)

    await client.patch(
        "/v1/modules/expenses/preferences", headers=account.headers, json={"enabled": False}
    )
    listed = await client.get(BUDGETS, headers=account.headers)
    deleted = await client.delete(f"{BUDGETS}/{made['id']}", headers=account.headers)

    assert anonymous.status_code == 401
    assert (listed.status_code, deleted.status_code) == (403, 403)
    assert listed.json()["code"] == "MODULE_DISABLED"
