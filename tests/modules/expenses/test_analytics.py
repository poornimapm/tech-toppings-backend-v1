"""``/v1/expenses/analytics``: every figure on known data, and the drill-down promise (the list
opened with a chart's filters totals what the chart showed)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from httpx import AsyncClient

from tests.helpers import Account, FrozenClock, register
from tests.modules.expenses.conftest import ENTRIES, add
from tests.modules.expenses.test_figures import today_of

ANALYTICS = "/v1/expenses/analytics"
SEPTEMBER = "from=2026-09-01&to=2026-09-30"

# August and September 2026. September 1st is a Tuesday.
SEED: list[dict[str, Any]] = [
    {"date": "2026-08-01", "kind": "income", "title": "Salary", "amount_minor": 5000000,
     "category_id": "salary"},
    {"date": "2026-08-10", "title": "Rice", "amount_minor": 50000, "category_id": "groceries",
     "merchant": "Big Bazaar", "quantity": 5, "unit": "kg"},
    {"date": "2026-08-20", "title": "Dinner", "amount_minor": 80000, "category_id": "food",
     "merchant": "Saravana", "for_whom": "Amma"},
    {"date": "2026-09-01", "kind": "income", "title": "Salary", "amount_minor": 5000000,
     "category_id": "salary"},
    {"date": "2026-09-02", "title": "Rice", "amount_minor": 60000, "category_id": "groceries",
     "merchant": "Big Bazaar", "quantity": 5, "unit": "kg"},
    {"date": "2026-09-02", "title": "Auto", "amount_minor": 8000, "category_id": "transport"},
    {"date": "2026-09-07", "title": "Dinner", "amount_minor": 120000, "category_id": "food",
     "merchant": "Saravana", "for_whom": "Amma"},
    {"date": "2026-09-15", "title": "Vegetables", "amount_minor": 25000,
     "category_id": "groceries", "merchant": "Big Bazaar", "for_whom": "Amma"},
    {"date": "2026-09-30", "title": "Shoes", "amount_minor": 250000, "category_id": "shopping",
     "merchant": "Bata"},
]  # fmt: skip


async def seeded(client: AsyncClient) -> Account:
    account = await register(client)
    for entry in SEED:
        await add(client, account, **entry)
    return account


async def get(client: AsyncClient, account: Account, path: str, query: str = SEPTEMBER) -> Any:
    response = await client.get(f"{ANALYTICS}/{path}?{query}", headers=account.headers)
    assert response.status_code == 200, response.text
    return response.json()


async def listed_total(client: AsyncClient, account: Account, query: str) -> int:
    """What the entries list (its totals line) shows for these filters."""
    response = await client.get(f"{ENTRIES}/summary?{query}", headers=account.headers)
    totals = response.json()["totals"]
    return int(totals[0]["expense_minor"]) if totals else 0


async def test_a_month_is_compared_with_the_same_days_of_the_month_before(
    client: AsyncClient,
) -> None:
    account = await seeded(client)

    found = await get(client, account, "summary")

    assert found == {
        "start": "2026-09-01",
        "end": "2026-09-30",
        "spent_minor": 463000,
        "received_minor": 5000000,
        "net_minor": 4537000,
        "count": 6,
        "currency": "INR",
        "days": 30,
        "avg_daily_spent_minor": 15433,
        "savings_rate_pct": 90.7,
        "previous": {"start": "2026-08-01", "end": "2026-08-30"},  # 30 days, as September
        "previous_totals": {
            "spent_minor": 130000,
            "received_minor": 5000000,
            "net_minor": 4870000,
            "count": 3,
        },
        "spent_change_pct": 256.2,
        "received_change_pct": 0.0,
    }


async def test_other_spans_are_compared_with_as_many_days_just_before(
    client: AsyncClient,
) -> None:
    account = await seeded(client)

    found = await get(client, account, "summary", "from=2026-09-02&to=2026-09-30")

    assert found["previous"] == {"start": "2026-08-04", "end": "2026-09-01"}
    assert (
        found["previous_totals"]["spent_minor"],
        found["previous_totals"]["received_minor"],
    ) == (
        130000,  # both August purchases
        5000000,  # and the 1 September salary
    )


async def test_shares_by_category_person_and_place(client: AsyncClient) -> None:
    account = await seeded(client)

    categories = await get(client, account, "by-category")
    people = await get(client, account, "by-person")
    places = await get(client, account, "top-merchants")
    income = await get(client, account, "by-category", f"{SEPTEMBER}&kind=income")

    assert (categories["kind"], categories["total_minor"]) == ("expense", 463000)
    assert [(i["category_id"], i["amount_minor"], i["share_pct"]) for i in categories["items"]] == [
        ("shopping", 250000, 54.0),
        ("food", 120000, 25.9),
        ("groceries", 85000, 18.4),
        ("transport", 8000, 1.7),
    ]
    assert [
        (i["name"], i["amount_minor"], i["count"], i["share_pct"]) for i in people["items"]
    ] == [
        (None, 318000, 3, 68.7),
        ("Amma", 145000, 2, 31.3),
    ]
    assert [(i["merchant"], i["amount_minor"]) for i in places["items"]] == [
        ("Bata", 250000),
        ("Saravana", 120000),
        ("Big Bazaar", 85000),
    ]  # the auto, with no place noted, isn't a place
    assert [(i["category_id"], i["amount_minor"]) for i in income["items"]] == [("salary", 5000000)]


async def test_a_drill_down_lists_exactly_what_the_chart_showed(client: AsyncClient) -> None:
    account = await seeded(client)
    categories = await get(client, account, "by-category")
    people = await get(client, account, "by-person")
    places = await get(client, account, "top-merchants")
    days = await get(client, account, "daily")

    for item in categories["items"]:
        query = f"{SEPTEMBER}&kind=expense&category_id={item['category_id']}"
        assert await listed_total(client, account, query) == item["amount_minor"]
    amma = next(item for item in people["items"] if item["name"] == "Amma")
    assert await listed_total(client, account, f"{SEPTEMBER}&for_whom=Amma") == amma["amount_minor"]
    for item in places["items"]:
        query = f"{SEPTEMBER}&merchant={item['merchant']}"
        assert await listed_total(client, account, query) == item["amount_minor"]
    second = next(point for point in days["points"] if point["date"] == "2026-09-02")
    assert (second["amount_minor"], second["count"]) == (68000, 2)
    assert await listed_total(client, account, "from=2026-09-02&to=2026-09-02") == 68000


async def test_every_day_and_every_month_are_there_even_when_empty(client: AsyncClient) -> None:
    account = await seeded(client)

    days = await get(client, account, "daily")
    months = await get(client, account, "monthly", "from=2026-08-01&to=2026-09-30")
    net = await get(client, account, "net", "from=2026-08-01&to=2026-09-30")

    assert len(days["points"]) == 30
    assert sum(point["amount_minor"] for point in days["points"]) == 463000
    assert days["points"][0] == {"date": "2026-09-01", "amount_minor": 0, "count": 0}
    assert [(m["month"], m["spent_minor"], m["received_minor"]) for m in months["months"]] == [
        ("2026-08", 130000, 5000000),
        ("2026-09", 463000, 5000000),
    ]
    assert [
        (m["net_minor"], m["savings_rate_pct"], m["cumulative_net_minor"]) for m in net["months"]
    ] == [
        (4870000, 97.4, 4870000),
        (4537000, 90.7, 9407000),
    ]
    assert net["total"] == {
        "spent_minor": 593000,
        "received_minor": 10000000,
        "net_minor": 9407000,
        "count": 9,
    }
    assert net["savings_rate_pct"] == 94.1


async def test_the_heatmap_lays_days_out_by_week_and_averages_weekdays(
    client: AsyncClient,
) -> None:
    account = await seeded(client)

    heat = await get(client, account, "weekday-heatmap")

    assert [week["start"] for week in heat["weeks"]] == [
        "2026-08-31", "2026-09-07", "2026-09-14", "2026-09-21", "2026-09-28",
    ]  # fmt: skip
    first, last = heat["weeks"][0]["days"], heat["weeks"][-1]["days"]
    assert first[0] is None  # Monday 31 August is outside September
    assert first[2] == {"date": "2026-09-02", "amount_minor": 68000}
    assert last[3:] == [None, None, None, None]
    by_weekday = {item["weekday"]: item for item in heat["weekdays"]}
    assert (by_weekday[0]["total_minor"], by_weekday[0]["average_minor"]) == (120000, 30000)
    assert (by_weekday[1]["total_minor"], by_weekday[1]["average_minor"]) == (25000, 5000)
    assert (by_weekday[2]["total_minor"], by_weekday[2]["average_minor"]) == (318000, 63600)


async def test_the_running_total_keeps_pace_with_the_days_before(client: AsyncClient) -> None:
    account = await seeded(client)

    running = await get(client, account, "cumulative")

    assert running["points"][0] == {"date": "2026-09-01", "total_minor": 0}
    assert running["points"][-1] == {"date": "2026-09-30", "total_minor": 463000}
    assert running["previous"] == {"start": "2026-08-01", "end": "2026-08-30"}
    assert running["previous_points"][-1] == {"date": "2026-08-30", "total_minor": 130000}
    assert len(running["previous_points"]) == len(running["points"])


async def test_top_products_say_what_was_spent_on_each(client: AsyncClient) -> None:
    account = await seeded(client)

    products = await get(client, account, "top-products", "from=2026-08-01&to=2026-09-30")

    assert [
        (item["name"], item["amount_minor"], item["count"], item["last_unit_price_minor"])
        for item in products["items"]
    ] == [("Rice", 110000, 2, 12000)]


async def test_an_expense_far_above_the_usual_for_its_category_is_flagged(
    client: AsyncClient,
) -> None:
    account = await register(client)
    for day, amount in zip(range(1, 8), (2000, 2200, 2400, 2000, 2100, 2300, 2200), strict=True):
        await add(client, account, title="Tea", amount_minor=amount, category_id="food",
                  date=f"2026-09-{day:02d}")  # fmt: skip
    party = await add(client, account, title="Party", amount_minor=15000, category_id="food",
                      date="2026-09-20")  # fmt: skip
    await add(client, account, title="Snack", amount_minor=2600, category_id="food",
              date="2026-09-21")  # fmt: skip
    await add(client, account, title="Laptop", amount_minor=8000000, category_id="shopping",
              date="2026-09-22")  # fmt: skip

    unusual = await get(client, account, "anomalies")

    assert unusual["items"] == [
        {
            "entry_id": party["id"],
            "title": "Party",
            "date": "2026-09-20",
            "category_id": "food",
            "amount_minor": 15000,
            "usual_minor": 2200,
            "times_usual": 6.8,
        }
    ]  # the snack is close to usual; the laptop has no shopping history to compare with


async def test_the_forecast_adds_the_days_left_at_the_recent_pace(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await register(client)
    today = today_of(clock)
    for back in range(1, 11):  # 10 quiet days of ₹10 before today
        await add(client, account, title="Tea", amount_minor=1000,
                  date=(today - timedelta(days=back)).isoformat())  # fmt: skip
    await add(client, account, title="Lunch", amount_minor=5000, date=today.isoformat())

    forecast = await get(client, account, "forecast", "")

    start = today.replace(day=1)
    month_end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    spent = 5000 + 1000 * sum(1 for back in range(1, 11) if today - timedelta(days=back) >= start)
    days_left = (month_end - today).days
    assert forecast | {"today": ""} == {
        "start": start.isoformat(),
        "end": month_end.isoformat(),
        "currency": "INR",
        "month": start.isoformat()[:7],
        "today": "",
        "spent_minor": spent,
        "days_left": days_left,
        "pace_daily_minor": 1000,
        "projected_minor": spent + 1000 * days_left,
        "low_minor": spent + 1000 * days_left,
        "high_minor": spent + 1000 * days_left,
        "lookback_days": 10,  # only as far back as the first expense
        "previous_month_minor": forecast["previous_month_minor"],
    }


async def test_the_forecast_band_reads_whole_weeks(client: AsyncClient, clock: FrozenClock) -> None:
    """Shopping once a week makes most single days zero; whole weeks still show the pace."""
    account = await register(client)
    today = today_of(clock)
    for back in (21, 14, 7):
        await add(client, account, title="Groceries", amount_minor=7000,
                  date=(today - timedelta(days=back)).isoformat())  # fmt: skip

    forecast = await get(client, account, "forecast", "")

    spent, days_left = forecast["spent_minor"], forecast["days_left"]
    assert forecast["pace_daily_minor"] == 1000
    assert (forecast["low_minor"], forecast["high_minor"]) == (
        spent + 1000 * days_left,
        spent + 1000 * days_left,
    )


async def test_a_past_month_is_its_own_forecast(client: AsyncClient) -> None:
    account = await seeded(client)

    forecast = await get(client, account, "forecast", "date=2026-09-10")

    assert (forecast["spent_minor"], forecast["days_left"], forecast["projected_minor"]) == (
        463000,
        0,
        463000,
    )
    assert forecast["previous_month_minor"] == 130000


async def test_figures_are_the_users_own(client: AsyncClient) -> None:
    await seeded(client)
    stranger = await register(client)

    found = await get(client, stranger, "summary")

    assert (found["spent_minor"], found["count"]) == (0, 0)


async def test_without_a_range_it_is_this_month_so_far(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await register(client)
    today = today_of(clock)
    await add(client, account, title="Tea", amount_minor=1500, date=today.isoformat())

    found = await get(client, account, "summary", "")

    assert (found["start"], found["end"]) == (today.replace(day=1).isoformat(), today.isoformat())
    assert found["spent_minor"] == 1500
    last_month = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    assert date.fromisoformat(found["previous"]["start"]) == last_month
