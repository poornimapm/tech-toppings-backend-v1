"""``/v1/expenses/recurring`` (U3): rules that add their own entries on each due date, exactly
once however often and however concurrently jobs run, and bills that remind until marked paid."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

import pytest
from beanie import PydanticObjectId
from fastapi import FastAPI
from httpx import AsyncClient

from app.modules.expenses.features.entries.models import Entry
from app.modules.expenses.features.recurring.models import RuleRepository
from app.modules.expenses.features.recurring.schemas import MarkPaidIn
from app.modules.expenses.features.recurring.service import Recurring
from app.modules.expenses.planning import planning
from tests.helpers import Account, FrozenClock, actor_of, register
from tests.modules.expenses.conftest import CATEGORIES, listed
from tests.modules.expenses.test_figures import today_of
from tests.platform.reminders.test_exactly_once import LONG_STEP, walk

RECURRING = "/v1/expenses/recurring"


def on(day: date, days: int = 0) -> str:
    return (day + timedelta(days=days)).isoformat()


def rule(
    anchor: str, *, title: str = "Electricity", unit: str = "month", **changes: Any
) -> dict[str, Any]:
    """A bill, ₹1,200 each month from ``anchor`` unless said otherwise."""
    body: dict[str, Any] = {
        "template": {"title": title, "amount_minor": 120000, "category_id": "utilities"},
        "cadence": {"unit": unit, "anchor": anchor},
        "mode": "remind",
    }
    return body | changes


async def create(client: AsyncClient, account: Account, anchor: str, **changes: Any) -> Any:
    response = await client.post(RECURRING, headers=account.headers, json=rule(anchor, **changes))
    assert response.status_code == 201, response.text
    return response.json()


async def change(client: AsyncClient, account: Account, rule_id: str, **body: Any) -> Any:
    response = await client.patch(f"{RECURRING}/{rule_id}", headers=account.headers, json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def rules(client: AsyncClient, account: Account) -> dict[str, Any]:
    page = (await client.get(f"{RECURRING}?page_size=100", headers=account.headers)).json()
    return {item["id"]: item for item in page["items"]}


async def made_by(client: AsyncClient, account: Account, rule_id: str) -> list[str]:
    """The days of the entries a rule added (or whose bill was paid), oldest first."""
    page = await listed(client, account)
    return sorted(item["date"] for item in page["items"] if item["recurring_id"] == rule_id)


async def reminded(client: AsyncClient, account: Account) -> list[tuple[str, str]]:
    page = (await client.get("/v1/reminders", headers=account.headers)).json()
    return [(item["params"]["title"], item["due_on"]) for item in page["items"]]


async def test_a_rule_adds_one_entry_per_due_date_however_often_jobs_run(
    cron_client: AsyncClient, clock: FrozenClock
) -> None:
    client = cron_client
    account = await register(client)
    today = today_of(clock)

    milk = await create(client, account, on(today), title="Milk", unit="week", mode="auto_create")
    first = await made_by(client, account, milk["id"])
    await walk(client, clock, account, hours=15 * 24, every=LONG_STEP)

    assert first == [on(today)]  # added as soon as it was set up
    assert await made_by(client, account, milk["id"]) == [on(today), on(today, 7), on(today, 14)]
    assert (await rules(client, account))[milk["id"]]["next_run"] == on(today, 21)
    entries = [item for item in (await listed(client, account))["items"] if item["title"] == "Milk"]
    assert {(item["source"], item["amount_minor"]) for item in entries} == {("recurring", 120000)}


async def test_runs_at_the_same_moment_add_a_due_date_once(
    app: FastAPI, client: AsyncClient, clock: FrozenClock
) -> None:
    account = await register(client)
    today = today_of(clock)
    milk = await create(client, account, on(today), title="Milk", unit="week", mode="auto_create")
    ctx = await actor_of(app, account)
    clock.advance(days=7)

    counts = await asyncio.gather(*(Recurring(ctx).materialize() for _ in range(10)))

    assert sum(counts) == 1
    assert await made_by(client, account, milk["id"]) == [on(today), on(today, 7)]
    assert (await rules(client, account))[milk["id"]]["next_run"] == on(today, 14)


async def test_after_a_long_time_away_only_the_latest_due_dates_are_added(
    app: FastAPI, client: AsyncClient, clock: FrozenClock
) -> None:
    account = await register(client)
    today = today_of(clock)
    paper = await create(client, account, on(today), title="Paper", unit="day", mode="auto_create")
    cap = planning().recurring.max_catch_up
    clock.advance(days=40)

    made = await Recurring(await actor_of(app, account)).materialize()

    assert made == cap
    latest = [on(today, days) for days in range(40 - cap + 1, 41)]
    assert await made_by(client, account, paper["id"]) == [on(today), *latest]
    assert (await rules(client, account))[paper["id"]]["next_run"] == on(today, 41)


async def test_nothing_before_today_is_added(client: AsyncClient, clock: FrozenClock) -> None:
    account = await register(client)
    today = today_of(clock)

    rent = await create(client, account, on(today, -14), unit="week", mode="auto_create")

    assert (rent["next_run"], rent["overdue"]) == (on(today, 7), False)
    assert await made_by(client, account, rent["id"]) == [on(today)]


async def test_a_switched_off_module_runs_no_job(client: AsyncClient, clock: FrozenClock) -> None:
    account = await register(client)
    today = today_of(clock)
    milk = await create(client, account, on(today), title="Milk", unit="week", mode="auto_create")
    preferences = "/v1/modules/expenses/preferences"
    bell = "/v1/notifications?page_size=1"

    async def added() -> int:
        mine = {"user_id": PydanticObjectId(account.user_id), "recurring_id": milk["id"]}
        return await Entry.find(mine).count()

    off = await client.patch(preferences, headers=account.headers, json={"enabled": False})
    clock.advance(days=7)
    await client.get(bell, headers=account.headers)
    waiting = await added()
    await client.patch(preferences, headers=account.headers, json={"enabled": True})
    await client.get(bell, headers=account.headers)

    assert (off.status_code, waiting) == (200, 1)
    assert await added() == 2


async def test_a_suggestion_confirmed_twice_makes_one_rule(client: AsyncClient) -> None:
    account = await register(client)
    signature = "expense|electricity|month"

    first = await client.post(
        RECURRING, headers=account.headers, json=rule("2030-01-05", signature=signature)
    )
    again = await client.post(
        RECURRING, headers=account.headers, json=rule("2030-02-07", signature=signature)
    )

    assert (first.status_code, again.status_code) == (201, 201)
    assert "Idempotent-Replayed" not in first.headers
    assert again.headers["Idempotent-Replayed"] == "true"
    assert again.json()["id"] == first.json()["id"]
    assert (again.json()["source"], again.json()["next_run"]) == ("detected", "2030-01-05")
    assert list(await rules(client, account)) == [first.json()["id"]]


async def test_a_bill_reminds_until_paused_or_deleted(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await register(client)
    today = today_of(clock)
    bill = await create(client, account, on(today, 5))
    waiting = await reminded(client, account)

    paused = await change(client, account, bill["id"], active=False)
    while_paused = await reminded(client, account)
    await change(client, account, bill["id"], active=True)
    back = await reminded(client, account)
    deleted = await client.delete(f"{RECURRING}/{bill['id']}", headers=account.headers)

    assert waiting == back == [("Electricity", on(today, 5))]
    assert (paused["active"], while_paused) == (False, [])
    assert deleted.status_code == 204
    assert (await reminded(client, account), await rules(client, account)) == ([], {})


async def test_marking_a_bill_paid_adds_its_entry_once_and_moves_it_on(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await register(client)
    today = today_of(clock)
    due = on(today, 3)
    bill = await create(client, account, due)
    url = f"{RECURRING}/{bill['id']}/mark-paid"
    paid = {"due_on": due, "amount_minor": 134500, "date": on(today, -1)}  # paid early

    first = await client.post(url, headers=account.headers, json=paid)
    again = await client.post(url, headers=account.headers, json={"due_on": due})
    moved = (await rules(client, account))[bill["id"]]

    assert (first.status_code, again.status_code) == (201, 201)
    assert again.headers["Idempotent-Replayed"] == "true"
    entry = first.json()
    assert again.json()["id"] == entry["id"]
    assert (entry["title"], entry["amount_minor"], entry["date"], entry["source"]) == (
        "Electricity",
        134500,
        on(today, -1),
        "manual",
    )
    assert entry["recurring_id"] == bill["id"]
    assert moved["next_run"] > due  # the next month's
    assert await reminded(client, account) == [("Electricity", moved["next_run"])]


@pytest.mark.parametrize("days", [1, 28])  # not a due date; a due date after the next one
async def test_only_a_due_date_up_to_the_next_can_be_paid(
    client: AsyncClient, clock: FrozenClock, days: int
) -> None:
    account = await register(client)
    due = today_of(clock) + timedelta(days=3)
    every_4_weeks = {"unit": "week", "every": 4, "anchor": due.isoformat()}
    bill = await create(client, account, due.isoformat(), cadence=every_4_weeks)

    response = await client.post(
        f"{RECURRING}/{bill['id']}/mark-paid",
        headers=account.headers,
        json={"due_on": on(due, days)},
    )

    assert response.status_code == 422, response.text
    assert (response.json()["code"], response.json()["details"]) == (
        "EXPENSE_RECURRING_NOT_DUE",
        {"next_run": due.isoformat()},
    )
    assert await made_by(client, account, bill["id"]) == []


async def test_a_new_pace_starts_afresh_from_today(client: AsyncClient, clock: FrozenClock) -> None:
    account = await register(client)
    today = today_of(clock)
    rent = await create(client, account, on(today, 2), unit="week")

    priced = await change(
        client, account, rent["id"], template=rent["template"] | {"amount_minor": 99900}
    )
    moved = await change(
        client, account, rent["id"], cadence={"unit": "week", "anchor": on(today, -3)}
    )

    assert (priced["next_run"], priced["template"]["amount_minor"]) == (on(today, 2), 99900)
    assert moved["next_run"] == on(today, 4)
    assert await reminded(client, account) == [("Electricity", on(today, 4))]


@pytest.mark.parametrize(
    "changes",
    [
        {"cadence": {"unit": "month", "every": 0, "anchor": "2030-01-01"}},
        {"cadence": {"unit": "month", "every": 13, "anchor": "2030-01-01"}},
        {"cadence": {"unit": "fortnight", "anchor": "2030-01-01"}},
        {"mode": "sometimes"},
        {"remind_days_before": 31},
        {"template": {"title": "Rent", "amount_minor": 0, "category_id": "housing"}},
        {"template": {"title": " ", "amount_minor": 1, "category_id": "housing"}},
        {"signature": ""},
        {"owner": "someone else"},
    ],
)
async def test_a_rule_must_make_sense(client: AsyncClient, changes: dict[str, Any]) -> None:
    account = await register(client)

    response = await client.post(
        RECURRING, headers=account.headers, json=rule("2030-01-01") | changes
    )

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_a_rule_needs_a_category_of_its_kind(client: AsyncClient) -> None:
    account = await register(client)
    salary = {"title": "Rent", "amount_minor": 100, "category_id": "salary"}  # an income one
    made = await create(client, account, "2030-01-01")

    added = await client.post(
        RECURRING, headers=account.headers, json=rule("2030-01-01", template=salary)
    )
    changed = await client.patch(
        f"{RECURRING}/{made['id']}", headers=account.headers, json={"template": salary}
    )

    for response in (added, changed):
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "EXPENSE_CATEGORY_INVALID"


async def test_a_rule_keeps_its_category_even_archived(
    app: FastAPI, client: AsyncClient, clock: FrozenClock
) -> None:
    """As entries do: what it adds, and a bill paid, are filed there still. A new rule can't
    choose it."""
    account = await register(client)
    today = today_of(clock)
    pets = await client.post(
        CATEGORIES, headers=account.headers, json={"name": "Pets", "icon": "🐶", "accent": "teal"}
    )
    dog_food = {"title": "Dog food", "amount_minor": 50000, "category_id": pets.json()["id"]}
    food = await create(
        client, account, on(today), unit="week", mode="auto_create", template=dog_food
    )
    vet = await create(client, account, on(today, 3), template=dog_food | {"title": "Vet"})
    await client.delete(f"{CATEGORIES}/{pets.json()['id']}", headers=account.headers)
    clock.advance(days=7)

    made = await Recurring(await actor_of(app, account)).materialize()
    priced = await change(
        client, account, vet["id"], template=vet["template"] | {"amount_minor": 70000}
    )
    paid = await client.post(
        f"{RECURRING}/{vet['id']}/mark-paid", headers=account.headers, json={"due_on": on(today, 3)}
    )
    refused = await client.post(
        RECURRING, headers=account.headers, json=rule(on(today, 9), template=dog_food)
    )

    assert made == 1
    assert await made_by(client, account, food["id"]) == [on(today), on(today, 7)]
    assert priced["template"]["category_id"] == pets.json()["id"]
    assert paid.status_code == 201, paid.text
    assert (paid.json()["category_id"], paid.json()["amount_minor"]) == (pets.json()["id"], 70000)
    assert (refused.status_code, refused.json()["code"]) == (422, "EXPENSE_CATEGORY_INVALID")


@pytest.mark.usefixtures("fx_rates")
async def test_a_rule_adds_in_its_own_currency(
    app: FastAPI, client: AsyncClient, clock: FrozenClock
) -> None:
    """After the user changes their base currency, a rule's amounts are never relabelled: it
    keeps adding in its own currency, each entry counted in the new base at its day's rate, and
    paying its bill works the same way."""
    account = await register(client)  # in rupees
    today = today_of(clock)
    milk = await create(client, account, on(today), title="Milk", unit="week", mode="auto_create")
    power = await create(client, account, on(today, 3), title="Power")
    moved = await client.patch("/v1/me", headers=account.headers, json={"base_currency": "USD"})
    bread = await create(client, account, on(today), title="Bread", unit="week", mode="auto_create")
    clock.advance(days=7)

    made = await Recurring(await actor_of(app, account)).materialize()
    paid = await client.post(
        f"{RECURRING}/{power['id']}/mark-paid",
        headers=account.headers,
        json={"due_on": on(today, 3)},
    )

    assert (moved.status_code, made) == (200, 2)
    assert await made_by(client, account, milk["id"]) == [on(today), on(today, 7)]
    assert await made_by(client, account, bread["id"]) == [on(today), on(today, 7)]
    assert paid.status_code == 201, paid.text
    page = await listed(client, account)
    assert {(item["title"], item["currency"], item["base_currency"]) for item in page["items"]} == {
        ("Milk", "INR", "INR"),  # added before the change: counted in rupees
        ("Milk", "INR", "USD"),
        ("Bread", "USD", "USD"),
        ("Power", "INR", "USD"),
    }
    assert (await rules(client, account))[milk["id"]]["currency"] == "INR"


async def test_a_bill_that_starts_adding_itself_adds_nothing_before_today(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await register(client)
    today = today_of(clock)
    bill = await create(client, account, on(today), unit="week")  # left unpaid for two weeks
    clock.advance(days=14)

    auto = await change(client, account, bill["id"], mode="auto_create")

    assert await made_by(client, account, bill["id"]) == [on(today, 14)]  # today's only
    assert (auto["next_run"], auto["overdue"]) == (on(today, 21), False)


async def test_a_new_pace_adds_todays_entry_once(client: AsyncClient, clock: FrozenClock) -> None:
    account = await register(client)
    today = today_of(clock)
    paper = await create(
        client, account, on(today, 2), title="Paper", unit="week", mode="auto_create"
    )

    daily = await change(
        client, account, paper["id"], cadence={"unit": "day", "anchor": on(today, -1)}
    )
    every_other = await change(
        client, account, paper["id"], cadence={"unit": "day", "every": 2, "anchor": on(today, -2)}
    )

    assert await made_by(client, account, paper["id"]) == [on(today)]
    assert (daily["next_run"], every_other["next_run"]) == (on(today, 1), on(today, 2))


async def test_a_change_at_the_same_moment_as_a_payment_keeps_it_paid(
    app: FastAPI, client: AsyncClient, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = await register(client)
    due = today_of(clock) + timedelta(days=5)
    bill = await create(client, account, due.isoformat())
    ctx = await actor_of(app, account)
    update = RuleRepository.update

    async def paid_meanwhile(self: RuleRepository, rule_id: Any, changes: Any) -> Any:
        """The bill is paid between the change's read and its write."""
        await Recurring(ctx).mark_paid(str(rule_id), MarkPaidIn(due_on=due))
        return await update(self, rule_id, changes)

    monkeypatch.setattr(RuleRepository, "update", paid_meanwhile)
    changed = await change(client, account, bill["id"], remind_days_before=1)

    assert changed["next_run"] > due.isoformat()  # the next month's
    assert await reminded(client, account) == [("Electricity", changed["next_run"])]


async def test_a_bill_paid_ahead_stays_paid_when_switched_back_on(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await register(client)
    today = today_of(clock)
    bill = await create(client, account, on(today, 3), unit="week")
    url = f"{RECURRING}/{bill['id']}/mark-paid"
    await client.post(url, headers=account.headers, json={"due_on": on(today, 3)})

    await change(client, account, bill["id"], active=False)
    back = await change(client, account, bill["id"], active=True)

    assert back["next_run"] == on(today, 10)
    assert await reminded(client, account) == [("Electricity", on(today, 10))]


async def test_an_earlier_due_date_can_be_paid_without_moving_the_bill(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await register(client)
    today = today_of(clock)
    bill = await create(client, account, on(today, -14), unit="week")  # first due today

    paid = await client.post(
        f"{RECURRING}/{bill['id']}/mark-paid",
        headers=account.headers,
        json={"due_on": on(today, -7)},
    )

    assert paid.status_code == 201, paid.text
    assert paid.json()["recurring_id"] == bill["id"]
    assert (await rules(client, account))[bill["id"]]["next_run"] == bill["next_run"] == on(today)
    assert await reminded(client, account) == [("Electricity", on(today))]
