"""Expenses and the budget: estimates and actual costs kept and undone from the history, naming only
the trip's own travellers and activities (rule 6), totalled by category in the trip's currency
through the platform's rates (a rate that can't be had leaves its amount unconverted, never
guessed), and who owes whom, in whole minor units, the same every time."""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from typing import Any, Protocol

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import AsyncClient

from app.core.config import Settings
from app.modules.travel.budget import settle, split
from app.modules.travel.rules import rules
from app.platform import FxRate
from tests.helpers import Account, FrozenClock, actor_of, register
from tests.modules.travel.conftest import (
    EXPENSES,
    FIRST_DAY,
    TRIPS,
    add_activity,
    add_expense,
    add_trip,
    history,
    stored,
    undo,
)
from tests.platform.fx.conftest import answer


async def budget_of(client: AsyncClient, account: Account, trip_id: str) -> dict[str, Any]:
    response = await client.get(f"{TRIPS}/{trip_id}/budget", headers=account.headers)
    assert response.status_code == 200, response.text
    return dict(response.json())


async def three_travel(client: AsyncClient, account: Account, **fields: Any) -> dict[str, Any]:
    """A trip for Asha, Ravi and Meena, in that order."""
    people = [{"name": name} for name in ("Asha", "Ravi", "Meena")]
    return await add_trip(client, account, travellers=people, **fields)


def ids(trip: dict[str, Any]) -> list[str]:
    return [traveller["id"] for traveller in trip["travellers"]]


def reasons(budget: dict[str, Any]) -> list[tuple[str, str]]:
    """Each unconverted amount's currency and why it has no rate."""
    return [(row["currency"], row["reason"]) for row in budget["unconverted"]]


# ---------------------------------------------------------------------- expenses


async def test_an_expense_is_added_edited_removed_and_undone(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)

    made = await add_expense(client, account, trip["id"], day=FIRST_DAY)
    assert (made["currency"], made["kind"], made["shared_with"]) == ("INR", "actual", [])
    url = f"{EXPENSES}/{made['id']}"
    edited = await client.patch(
        url, headers=account.headers, json={"amount_minor": 2500, "revision_no": 1}
    )
    assert edited.status_code == 200, edited.text
    assert (edited.json()["amount_minor"], edited.json()["title"]) == (2500, "Dinner")
    stale = await client.patch(
        url, headers=account.headers, json={"amount_minor": 1, "revision_no": 1}
    )
    assert (stale.status_code, stale.json()["code"]) == (409, "VERSION_CONFLICT")

    removed = await client.delete(url, headers=account.headers)
    assert removed.status_code == 200, removed.text
    listed = await client.get(f"{TRIPS}/{trip['id']}/expenses", headers=account.headers)
    assert listed.json() == []
    removal = (await history(client, account, trip["id"]))[0]
    assert removed.json() == {"event_id": removal["id"]}
    assert [item["kind"] for item in await history(client, account, trip["id"])][:3] == [
        "expense_removed",
        "expense_edited",
        "expense_added",
    ]
    assert (await undo(client, account, removal["id"])).status_code == 200
    [back] = (await client.get(f"{TRIPS}/{trip['id']}/expenses", headers=account.headers)).json()
    assert (back["id"], back["amount_minor"]) == (made["id"], 2500)


async def test_an_expense_names_only_the_trips_own_travellers_and_activities(
    client: AsyncClient,
) -> None:
    account = await register(client)
    trip, other = await three_travel(client, account), await three_travel(client, account)
    elsewhere = await add_activity(client, account, other["id"])
    url = f"{TRIPS}/{trip['id']}/expenses"
    body = {"kind": "actual", "category": "food", "title": "Lunch", "amount_minor": 300}

    sent: list[tuple[dict[str, Any], str]] = [
        ({"paid_by": ids(other)[0]}, "TRAVEL_TRAVELLER_UNKNOWN"),
        ({"shared_with": [ids(trip)[0], ids(other)[1]]}, "TRAVEL_TRAVELLER_UNKNOWN"),
        ({"activity_id": elsewhere["id"]}, "TRAVEL_ACTIVITY_UNKNOWN"),
        ({"activity_id": "0" * 24}, "TRAVEL_ACTIVITY_UNKNOWN"),
    ]
    for fields, code in sent:
        response = await client.post(url, headers=account.headers, json={**body, **fields})
        assert (response.status_code, response.json()["code"]) == (422, code), fields

    assert (await stored(account))["travel_expenses"] == []


@pytest.mark.parametrize(
    "fields",
    [
        {"category": "souvenirs"},
        {"kind": "guess"},
        {"amount_minor": -1},
        {"currency": "rupees"},
        {"shared_with": ["same", "same"]},
    ],
)
async def test_what_an_expense_can_not_be(client: AsyncClient, fields: dict[str, Any]) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    body = {"kind": "actual", "category": "food", "title": "Lunch", "amount_minor": 300, **fields}

    response = await client.post(
        f"{TRIPS}/{trip['id']}/expenses", headers=account.headers, json=body
    )

    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_ERROR")


async def test_a_traveller_stays_while_an_expense_names_them(client: AsyncClient) -> None:
    account = await register(client)
    trip = await three_travel(client, account)
    asha, ravi, meena = trip["travellers"]
    await add_expense(client, account, trip["id"], paid_by=asha["id"], shared_with=[meena["id"]])

    refused = await client.patch(
        f"{TRIPS}/{trip['id']}",
        headers=account.headers,
        json={"travellers": [asha, ravi], "revision_no": 1},
    )

    assert (refused.status_code, refused.json()["code"]) == (409, "TRAVEL_TRAVELLER_IN_USE")
    assert refused.json()["details"] == {"traveller_ids": [meena["id"]]}


async def test_an_expense_is_not_brought_back_naming_someone_who_left(client: AsyncClient) -> None:
    account = await register(client)
    trip = await three_travel(client, account)
    asha, ravi, meena = trip["travellers"]
    expense = await add_expense(client, account, trip["id"], paid_by=meena["id"])
    await client.delete(f"{EXPENSES}/{expense['id']}", headers=account.headers)
    removal = (await history(client, account, trip["id"]))[0]
    left = await client.patch(
        f"{TRIPS}/{trip['id']}",
        headers=account.headers,
        json={"travellers": [asha, ravi], "revision_no": 1},
    )
    assert left.status_code == 200, left.text

    refused = await undo(client, account, removal["id"])

    assert (refused.status_code, refused.json()["code"]) == (409, "TRAVEL_UNDO_STALE")


async def test_an_activitys_adding_is_not_undone_while_an_expense_is_tied_to_it(
    client: AsyncClient,
) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    activity = await add_activity(client, account, trip["id"])
    adding = (await history(client, account, trip["id"]))[0]
    expense = await add_expense(client, account, trip["id"], activity_id=activity["id"])

    refused = await undo(client, account, adding["id"])

    assert (refused.status_code, refused.json()["code"]) == (409, "TRAVEL_UNDO_STALE")
    assert refused.json()["details"] == {"record_ids": [activity["id"]]}
    await client.delete(f"{EXPENSES}/{expense['id']}", headers=account.headers)
    assert (await undo(client, account, adding["id"])).status_code == 200


async def test_a_mode_switch_leaves_bookings_and_expenses_as_they_are(
    client: AsyncClient,
) -> None:
    """Rule 7, for what this part adds."""
    account = await register(client)
    trip = await three_travel(client, account)
    await client.post(
        f"{TRIPS}/{trip['id']}/bookings",
        headers=account.headers,
        json={"kind": "hotel", "title": "Stay"},
    )
    await add_expense(client, account, trip["id"], paid_by=ids(trip)[0], shared_with=ids(trip))
    before = await stored(account)

    switched = await client.patch(
        f"{TRIPS}/{trip['id']}", headers=account.headers, json={"mode": "basic", "revision_no": 1}
    )

    assert switched.status_code == 200, switched.text
    after = await stored(account)
    for name in (
        "travel_bookings",
        "travel_booking_revisions",
        "travel_expenses",
        "travel_expense_revisions",
        "travel_events",
    ):
        assert after[name] == before[name], name


# ---------------------------------------------------------------------- the budget


async def test_estimated_against_actual_by_category_and_what_remains(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account, budget_minor=100000)
    for kind, category, amount in [
        ("estimate", "stay", 40000),
        ("actual", "stay", 42000),
        ("estimate", "food", 10000),
        ("actual", "food", 3000),
        ("actual", "food", 4500),
        ("actual", "other", 500),
    ]:
        await add_expense(
            client, account, trip["id"], kind=kind, category=category, amount_minor=amount
        )

    found = await budget_of(client, account, trip["id"])

    assert (found["currency"], found["budget_minor"]) == ("INR", 100000)
    assert (found["estimated_minor"], found["actual_minor"]) == (50000, 50000)
    assert found["remaining_minor"] == 50000
    assert found["categories"] == [  # in the order the module's data lists them
        {"category": "stay", "estimated_minor": 40000, "actual_minor": 42000},
        {"category": "food", "estimated_minor": 10000, "actual_minor": 7500},
        {"category": "other", "estimated_minor": 0, "actual_minor": 500},
    ]
    assert found["unconverted"] == []


async def test_a_trip_with_no_expenses_and_no_budget_is_an_honest_empty_answer(
    client: AsyncClient,
) -> None:
    account = await register(client)
    trip = await three_travel(client, account)

    found = await budget_of(client, account, trip["id"])

    assert (found["estimated_minor"], found["actual_minor"], found["remaining_minor"]) == (
        0,
        0,
        None,
    )
    assert (found["categories"], found["settlements"]) == ([], [])
    assert [row["balance_minor"] for row in found["balances"]] == [0, 0, 0]


async def test_a_shared_cost_is_split_to_the_minor_unit(client: AsyncClient) -> None:
    """₹10.00 among three is 3.34, 3.33 and 3.33: the paisa left over goes to the first of them in
    the trip's order, so the parts add up to exactly what was paid."""
    account = await register(client)
    trip = await three_travel(client, account)
    asha, ravi, meena = ids(trip)
    await add_expense(
        client,
        account,
        trip["id"],
        amount_minor=1000,
        paid_by=meena,
        shared_with=[meena, ravi, asha],
    )

    found = await budget_of(client, account, trip["id"])

    assert found["balances"] == [
        {"traveller_id": asha, "paid_minor": 0, "share_minor": 334, "balance_minor": -334},
        {"traveller_id": ravi, "paid_minor": 0, "share_minor": 333, "balance_minor": -333},
        {"traveller_id": meena, "paid_minor": 1000, "share_minor": 333, "balance_minor": 667},
    ]
    assert found["settlements"] == [
        {"from_traveller_id": asha, "to_traveller_id": meena, "amount_minor": 334},
        {"from_traveller_id": ravi, "to_traveller_id": meena, "amount_minor": 333},
    ]


async def test_who_owes_whom_counts_only_what_was_really_paid_by_someone(
    client: AsyncClient,
) -> None:
    account = await register(client)
    trip = await three_travel(client, account)
    asha, ravi, meena = ids(trip)
    everyone = [asha, ravi, meena]
    await add_expense(
        client, account, trip["id"], amount_minor=9000, paid_by=asha, shared_with=everyone
    )
    await add_expense(
        client, account, trip["id"], amount_minor=3000, paid_by=ravi, shared_with=everyone
    )
    await add_expense(client, account, trip["id"], amount_minor=500, paid_by=meena)  # her own
    await add_expense(
        client, account, trip["id"], kind="estimate", paid_by=asha, shared_with=everyone
    )
    await add_expense(
        client, account, trip["id"], amount_minor=700, shared_with=everyone
    )  # who paid?

    first = await budget_of(client, account, trip["id"])
    again = await budget_of(client, account, trip["id"])

    assert first == again  # the same records, the same answer
    assert [row["balance_minor"] for row in first["balances"]] == [5000, -1000, -4000]
    assert [row["share_minor"] for row in first["balances"]] == [4000, 4000, 4500]
    assert first["settlements"] == [
        {"from_traveller_id": meena, "to_traveller_id": asha, "amount_minor": 4000},
        {"from_traveller_id": ravi, "to_traveller_id": asha, "amount_minor": 1000},
    ]


@pytest.mark.usefixtures("fx_rates")
async def test_another_currency_is_converted_at_its_days_rate(client: AsyncClient) -> None:
    """$10 at 83.5 (tests.platform.fx.conftest) is ₹835, shared like any other cost."""
    account = await register(client)
    trip = await three_travel(client, account)
    asha, ravi, _ = ids(trip)
    await add_expense(
        client,
        account,
        trip["id"],
        amount_minor=1000,
        currency="USD",
        day="2026-09-05",
        paid_by=asha,
        shared_with=[asha, ravi],
    )

    found = await budget_of(client, account, trip["id"])

    assert (found["actual_minor"], found["unconverted"]) == (83500, [])
    assert [row["balance_minor"] for row in found["balances"]] == [41750, -41750, 0]


async def test_an_amount_with_no_rate_is_listed_unconverted_and_counted_nowhere(
    client: AsyncClient, fx_rates: respx.Route, keep_rate: Keep
) -> None:
    account = await register(client)
    await keep_rate("EUR", "2010-01-01", {"INR": "90", "GBP": "0.85"})  # the provider has GBP
    trip = await three_travel(client, account, budget_minor=50000)
    asha, ravi, _ = ids(trip)
    shared = {"paid_by": asha, "shared_with": [asha, ravi]}
    await add_expense(client, account, trip["id"], amount_minor=2000, **shared)
    odd = await add_expense(client, account, trip["id"], currency="XYZ", title="Odd", **shared)
    away = await add_expense(
        client, account, trip["id"], kind="estimate", currency="GBP", title="Away", **shared
    )

    def pounds_unreachable(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("base") == "GBP":  # and never fetched before: none kept
            raise httpx.ConnectError("down")
        return answer(request)  # XYZ: no such currency

    fx_rates.side_effect = pounds_unreachable
    found = await budget_of(client, account, trip["id"])

    assert found["unconverted"] == [
        {
            "expense_id": odd["id"],
            "title": "Odd",
            "kind": "actual",
            "category": "food",
            "amount_minor": 1000,
            "currency": "XYZ",
            "reason": "unsupported",
        },
        {
            "expense_id": away["id"],
            "title": "Away",
            "kind": "estimate",
            "category": "food",
            "amount_minor": 1000,
            "currency": "GBP",
            "reason": "unavailable",
        },
    ]
    assert (found["actual_minor"], found["estimated_minor"]) == (2000, 0)  # no guessed rate
    assert found["remaining_minor"] == 48000
    assert found["categories"] == [{"category": "food", "estimated_minor": 0, "actual_minor": 2000}]
    assert [row["balance_minor"] for row in found["balances"]] == [1000, -1000, 0]


class Keep(Protocol):
    async def __call__(
        self, base: str, day: str, rates: dict[str, str], fetched_at: dt.datetime | None = None
    ) -> None: ...


@pytest.fixture
async def keep_rate(
    client: AsyncClient,  # noqa: ARG001 - requested so the app (and its database) is up
) -> AsyncIterator[Keep]:
    """Rates written straight to the platform's shared store, as fetched before (``fetched_at``,
    default now); gone after."""
    made: list[FxRate] = []

    async def keep(
        base: str, day: str, rates: dict[str, str], fetched_at: dt.datetime | None = None
    ) -> None:
        when = fetched_at or dt.datetime.now(dt.UTC)
        rate = FxRate(base=base, day=day, rate_date=day, rates=rates, fetched_at=when)
        made.append(await rate.insert())

    yield keep
    for rate in made:
        await rate.delete()


async def test_an_older_days_rate_is_never_taken_for_an_expenses_own(
    client: AsyncClient, fx_rates: respx.Route, keep_rate: Keep
) -> None:
    """With the provider down, the platform answers the latest rate it keeps, however old: the
    budget doesn't take it for the expense's day, and says so."""
    account = await register(client)
    trip = await add_trip(client, account)
    await keep_rate("GBP", "2004-06-01", {"INR": "50"})
    away = await add_expense(client, account, trip["id"], currency="GBP", day="2005-01-10")
    fx_rates.side_effect = httpx.ConnectError("down")

    found = await budget_of(client, account, trip["id"])

    assert found["actual_minor"] == 0
    assert [(row["expense_id"], row["reason"]) for row in found["unconverted"]] == [
        (away["id"], "unavailable")
    ]


async def test_once_the_provider_fails_budgets_leave_it_be_for_a_while(
    client: AsyncClient, clock: FrozenClock, fx_rates: respx.Route, keep_rate: Keep
) -> None:
    """A week of costs in euros, the provider down: one load waits on it once, the next not at
    all, and a rate kept for a cost's own day still counts meanwhile."""
    account = await register(client)
    trip = await add_trip(client, account)
    await keep_rate("EUR", "2011-02-01", {"INR": "90"})
    for day in range(1, 8):
        await add_expense(client, account, trip["id"], currency="EUR", day=f"2011-02-0{day}")
    fx_rates.side_effect = httpx.ConnectError("down")

    first = await budget_of(client, account, trip["id"])
    assert (first["actual_minor"], len(first["unconverted"])) == (90000, 6)
    assert fx_rates.call_count == 1

    again = await budget_of(client, account, trip["id"])
    assert again == first
    assert fx_rates.call_count == 1

    clock.advance(seconds=rules().budget.fx_rest_seconds + 1)
    await budget_of(client, account, trip["id"])
    assert fx_rates.call_count == 2


async def test_todays_rate_kept_but_not_refreshed_rests_the_provider_too(
    app: FastAPI,
    client: AsyncClient,
    clock: FrozenClock,
    settings: Settings,
    fx_rates: respx.Route,
    keep_rate: Keep,
) -> None:
    """Today's rate was fetched this morning; by evening it is past the cache's time, and the
    provider is down. The platform answers the one it kept, still today's own, so it counts; but
    asking it again failed, so budgets leave the provider be for a while all the same."""
    account = await register(client)
    today = (await actor_of(app, account)).today().isoformat()
    morning = clock.now() - dt.timedelta(hours=settings.fx.cache_ttl_hours + 1)
    await keep_rate("CHF", today, {"INR": "95"}, fetched_at=morning)
    trip = await add_trip(client, account)
    await add_expense(client, account, trip["id"], currency="CHF")  # no day: today's rate
    fx_rates.side_effect = httpx.ConnectError("down")

    first = await budget_of(client, account, trip["id"])
    assert (first["actual_minor"], first["unconverted"]) == (95000, [])
    assert fx_rates.call_count == 1

    again = await budget_of(client, account, trip["id"])
    assert again == first
    assert fx_rates.call_count == 1  # not waited on again


async def test_a_currency_the_provider_lacks_is_said_to_be_so_while_it_rests(
    client: AsyncClient, fx_rates: respx.Route, keep_rate: Keep
) -> None:
    """XYZ was answered as no such currency; then pounds failed. While budgets leave the provider
    be, XYZ is still one it lacks, not one whose rate can't be had just now."""
    account = await register(client)
    await keep_rate("EUR", "2012-01-01", {"INR": "90", "GBP": "0.85"})  # the provider has GBP
    trip = await add_trip(client, account)
    await add_expense(client, account, trip["id"], currency="XYZ", day="2012-01-02")
    await add_expense(client, account, trip["id"], currency="GBP", day="2012-01-02")

    def pounds_unreachable(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("base") == "GBP":
            raise httpx.ConnectError("down")
        return answer(request)  # XYZ: no such currency

    fx_rates.side_effect = pounds_unreachable
    first = await budget_of(client, account, trip["id"])
    again = await budget_of(client, account, trip["id"])

    for found in (first, again):
        assert reasons(found) == [("XYZ", "unsupported"), ("GBP", "unavailable")]
    assert fx_rates.call_count == 2  # both asked once, by the first load alone


async def test_while_it_rests_a_currency_no_kept_rates_name_is_one_it_lacks(
    client: AsyncClient, fx_rates: respx.Route, keep_rate: Keep
) -> None:
    """Every answer of the provider names all the currencies it has, so one that none of the rates
    kept names is one it lacks, though this process never asked it."""
    account = await register(client)
    await keep_rate("GBP", "2013-05-01", {"INR": "100", "EUR": "1.1", "USD": "1.3"})
    trip = await add_trip(client, account)
    await add_expense(client, account, trip["id"], currency="GBP", day="2013-05-02")
    await add_expense(client, account, trip["id"], currency="AED", day="2013-05-02")
    fx_rates.side_effect = httpx.ConnectError("down")

    found = await budget_of(client, account, trip["id"])

    assert reasons(found) == [("GBP", "unavailable"), ("AED", "unsupported")]
    assert fx_rates.call_count == 1  # pounds failed: the dirham was never asked


async def test_the_cost_that_finds_the_provider_down_is_told_as_those_after_it(
    client: AsyncClient, fx_rates: respx.Route, keep_rate: Keep
) -> None:
    """The dirham's first day is asked of the provider, down; its second is looked up while
    budgets leave it be. No rates kept name it, so both are a currency it lacks, on every load."""
    account = await register(client)
    await keep_rate("GBP", "2015-05-01", {"INR": "100", "EUR": "1.1", "USD": "1.3"})
    trip = await add_trip(client, account)
    for day in ("2015-05-02", "2015-05-03"):
        await add_expense(client, account, trip["id"], currency="AED", day=day)
    fx_rates.side_effect = httpx.ConnectError("down")

    first = await budget_of(client, account, trip["id"])
    again = await budget_of(client, account, trip["id"])

    for found in (first, again):
        assert reasons(found) == [("AED", "unsupported"), ("AED", "unsupported")]
    assert fx_rates.call_count == 1


async def test_a_trip_kept_in_a_currency_the_provider_lacks_is_said_to_be_so_while_it_rests(
    client: AsyncClient, fx_rates: respx.Route, keep_rate: Keep
) -> None:
    """The provider's euro rates never name dirhams, so a cost in euros on a trip kept in them
    has no rate whether budgets leave the provider be or not, and is said to be so either way."""
    account = await register(client)
    await keep_rate("EUR", "2014-01-01", {"INR": "90", "USD": "1.09", "GBP": "0.85"})
    dirhams = await add_trip(client, account, currency="AED")
    await add_expense(client, account, dirhams["id"], currency="EUR", day="2014-01-03")
    before = await budget_of(client, account, dirhams["id"])  # asked: no dirhams in its answer
    rupees = await add_trip(client, account)
    await add_expense(client, account, rupees["id"], currency="GBP", day="2014-01-03")
    fx_rates.side_effect = httpx.ConnectError("down")
    pounds = await budget_of(client, account, rupees["id"])  # fails: budgets leave it be
    await add_expense(client, account, dirhams["id"], currency="EUR", day="2014-01-04")

    during = await budget_of(client, account, dirhams["id"])

    assert reasons(before) == [("EUR", "unsupported")]
    assert reasons(pounds) == [("GBP", "unavailable")]
    assert reasons(during) == [("EUR", "unsupported"), ("EUR", "unsupported")]
    assert fx_rates.call_count == 2  # the euros' first day, and the pounds


async def test_another_persons_budget_is_out_of_reach(
    client: AsyncClient, other_client: AsyncClient
) -> None:
    owner, intruder = await register(client), await register(other_client)
    trip = await add_trip(client, owner)
    await add_expense(client, owner, trip["id"])

    found = await other_client.get(f"{TRIPS}/{trip['id']}/budget", headers=intruder.headers)

    assert found.status_code == 404


# ---------------------------------------------------------------------- splitting and settling


@pytest.mark.parametrize(
    ("amount", "sharers", "parts"),
    [
        (1000, ["a", "b", "c"], {"a": 334, "b": 333, "c": 333}),
        (1001, ["a", "b", "c"], {"a": 334, "b": 334, "c": 333}),
        (999, ["a", "b", "c"], {"a": 333, "b": 333, "c": 333}),
        (1, ["a", "b"], {"a": 1, "b": 0}),
        (0, ["a", "b"], {"a": 0, "b": 0}),
        (7, ["a"], {"a": 7}),
    ],
)
def test_a_split_adds_up_to_the_minor_unit(
    amount: int, sharers: list[str], parts: dict[str, int]
) -> None:
    assert split(amount, sharers) == parts
    assert sum(parts.values()) == amount


def test_settling_pays_the_largest_debt_to_the_largest_credit_ties_by_order() -> None:
    order = ["a", "b", "c", "d"]

    transfers = settle({"a": 300, "b": -100, "c": -100, "d": -100}, order)
    tied = settle({"a": -100, "b": -100, "c": 100, "d": 100}, order)

    assert [(t.from_traveller_id, t.to_traveller_id, t.amount_minor) for t in transfers] == [
        ("b", "a", 100),
        ("c", "a", 100),
        ("d", "a", 100),
    ]
    assert [(t.from_traveller_id, t.to_traveller_id, t.amount_minor) for t in tied] == [
        ("a", "c", 100),
        ("b", "d", 100),
    ]
    assert settle({"a": 0, "b": 0}, ["a", "b"]) == []


def test_settling_evens_everyone_up_in_fewer_transfers_than_travellers() -> None:
    order = ["a", "b", "c", "d", "e"]
    balances = {"a": 4567, "b": -1234, "c": -2000, "d": 1, "e": -1334}

    transfers = settle(balances, order)

    left = dict(balances)
    for transfer in transfers:
        assert transfer.amount_minor > 0
        left[transfer.from_traveller_id] += transfer.amount_minor
        left[transfer.to_traveller_id] -= transfer.amount_minor
    assert set(left.values()) == {0}
    assert len(transfers) <= len(order) - 1
    assert settle(balances, order) == transfers  # the same every time
