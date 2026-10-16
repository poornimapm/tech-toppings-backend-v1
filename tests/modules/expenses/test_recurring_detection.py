"""``GET /v1/expenses/recurring/suggestions``: what repeats in the user's own entries, at a
steady pace and amount, offered as a rule to confirm; never what a rule is already about, what
the user said isn't regular, or what a rule added itself."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient

from app.modules.expenses.features.recurring.service import Recurring
from tests.helpers import Account, FrozenClock, actor_of, register
from tests.modules.expenses.conftest import add
from tests.modules.expenses.test_figures import today_of
from tests.modules.expenses.test_recurring import RECURRING, create, on

SUGGESTIONS = f"{RECURRING}/suggestions"


async def seen(
    client: AsyncClient, account: Account, today: date, title: str, *days_ago: int, **fields: Any
) -> None:
    """``title`` on each of those days (₹125 unless said otherwise)."""
    for days in days_ago:
        await add(client, account, title=title, date=on(today, -days), **fields)


async def suggested(client: AsyncClient, account: Account) -> list[dict[str, Any]]:
    response = await client.get(SUGGESTIONS, headers=account.headers)
    assert response.status_code == 200, response.text
    return list(response.json())


async def test_what_repeats_steadily_is_suggested(client: AsyncClient, clock: FrozenClock) -> None:
    account = await register(client)
    today = today_of(clock)
    rent = {"category_id": "housing", "amount_minor": 1500000, "merchant": "Mr Rao"}
    await seen(client, account, today, "House rent", 93, 62, 31, 2, **rent)
    await add(client, account, title="Milk", date=on(today, -22), amount_minor=6000)
    await add(client, account, title="milk ", date=on(today, -15), amount_minor=6200)
    await add(client, account, title="Milk", date=on(today, -8), amount_minor=5900)
    await add(client, account, title="Milk", date=on(today, -1), amount_minor=6000, note="Late")
    await seen(client, account, today, "Movie", 50, 31, 9, 3)  # now and then
    for days, amount in ((90, 200000), (60, 260000), (30, 330000), (1, 400000)):  # creeping up
        await add(client, account, title="Petrol", date=on(today, -days), amount_minor=amount)
    await seen(client, account, today, "Gym", 400, 370, 340)  # stopped a year ago

    found = await suggested(client, account)

    assert [(item["signature"], item["count"], item["last_on"]) for item in found] == [
        ("expense|house rent|month", 4, on(today, -2)),
        ("expense|milk|week", 4, on(today, -1)),
    ]
    rent_found, milk = found
    assert rent_found["template"] | {"amount_minor": None} == {
        "kind": "expense",
        "title": "House rent",
        "amount_minor": None,
        "category_id": "housing",
        "merchant": "Mr Rao",
        "payment_mode": None,
        "for_whom": None,
        "tags": [],
        "note": None,
    }
    assert rent_found["template"]["amount_minor"] == 1500000
    # The usual amount, as last written, and anchored on its next due date.
    assert (milk["template"]["title"], milk["template"]["amount_minor"]) == ("Milk", 6000)
    assert milk["template"]["note"] is None
    assert milk["cadence"] == {"unit": "week", "every": 1, "anchor": on(today, 6)}
    assert rent_found["cadence"]["unit"] == "month"
    assert on(today) <= rent_found["cadence"]["anchor"] <= on(today, 31)


async def test_dismissed_and_covered_suggestions_are_hidden(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await register(client)
    today = today_of(clock)
    await seen(client, account, today, "Milk", 15, 8, 1)
    await seen(client, account, today, "Rent", 62, 31, 2, amount_minor=1500000)
    await seen(client, account, today, "Water", 62, 31, 2, amount_minor=30000)
    milk, rent, water = await suggested(client, account)  # seen as often: by signature
    dismiss = {"signature": milk["signature"]}

    first = await client.post(f"{SUGGESTIONS}/dismiss", headers=account.headers, json=dismiss)
    again = await client.post(f"{SUGGESTIONS}/dismiss", headers=account.headers, json=dismiss)
    confirmed = await client.post(
        RECURRING,
        headers=account.headers,
        json={
            "template": rent["template"],
            "cadence": rent["cadence"],
            "mode": "remind",
            "signature": rent["signature"],
        },
    )
    # A rule set up by hand about the same thing covers it too.
    await create(client, account, water["cadence"]["anchor"], title="WATER")
    left = await suggested(client, account)
    deleted = await client.delete(f"{RECURRING}/{confirmed.json()['id']}", headers=account.headers)

    assert (first.status_code, again.status_code, confirmed.status_code) == (204, 204, 201)
    assert (confirmed.json()["source"], confirmed.json()["next_run"]) == (
        "detected",
        rent["cadence"]["anchor"],
    )
    assert left == []
    # Deleting a confirmed rule says it isn't regular any more: not suggested again.
    assert deleted.status_code == 204
    assert await suggested(client, account) == []


async def test_what_a_rule_added_is_not_suggested(
    app: FastAPI, client: AsyncClient, clock: FrozenClock
) -> None:
    account = await register(client)
    today = today_of(clock)
    paper = await create(client, account, on(today), title="Paper", unit="week", mode="auto_create")
    ctx = await actor_of(app, account)
    for _ in range(3):
        clock.advance(days=7)
        await Recurring(ctx).materialize()

    await client.delete(f"{RECURRING}/{paper['id']}", headers=account.headers)

    assert await suggested(client, account) == []


async def test_a_yearly_repeat_is_seen(client: AsyncClient, clock: FrozenClock) -> None:
    account = await register(client)
    today = today_of(clock)
    last = today - timedelta(days=2)
    await seen(client, account, today, "Car insurance", 730, 365, 2, amount_minor=1200000)

    (insurance,) = await suggested(client, account)

    assert (insurance["signature"], insurance["count"]) == ("expense|car insurance|year", 3)
    assert insurance["cadence"]["anchor"] == last.replace(year=last.year + 1).isoformat()


async def test_one_entry_a_day_counts_once(client: AsyncClient, clock: FrozenClock) -> None:
    account = await register(client)
    today = today_of(clock)
    await seen(client, account, today, "Milk", 15, 8, 8, 1, 1)

    (milk,) = await suggested(client, account)

    assert (milk["count"], milk["cadence"]["anchor"]) == (3, on(today, 6))
    assert date.fromisoformat(milk["last_on"]) == today - timedelta(days=1)
