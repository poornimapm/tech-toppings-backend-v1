"""Entries in other currencies (ADR-0004): each keeps what it was in the user's base currency on
its day (``fx_rate``, ``amount_base_minor``), and every total counts that, so totals never move
when rates do."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from httpx import AsyncClient

from tests.helpers import Account, FrozenClock, register
from tests.modules.expenses.conftest import ENTRIES, add, post, save, signed_up_at
from tests.platform.fx.conftest import RATES


async def hotel(client: AsyncClient, account: Account, **changes: Any) -> dict[str, Any]:
    """120 dollars on 5 September: 1 USD bought 83.5 INR (tests.platform.fx.conftest)."""
    return await add(
        client,
        account,
        **{"title": "Hotel", "currency": "USD", "amount_minor": 12000, "date": "2026-09-05"}
        | changes,
    )


@pytest.mark.usefixtures("fx_rates")
async def test_an_entry_keeps_what_it_was_in_my_currency(client: AsyncClient) -> None:
    account = await register(client)  # in rupees
    made = await hotel(client, account)
    rice = await add(client, account, title="Rice", amount_minor=12500, date="2026-09-06")

    assert (made["currency"], made["amount_minor"]) == ("USD", 12000)
    assert (made["base_currency"], made["fx_rate"], made["amount_base_minor"]) == (
        "INR",
        "83.5",
        1002000,
    )
    assert (rice["base_currency"], rice["fx_rate"], rice["amount_base_minor"]) == (
        "INR",
        "1",
        12500,
    )

    # Every total counts the rupees: the list's, the overview's, and a question's.
    days = {"from": "2026-09-01", "to": "2026-09-30"}
    summary = (await client.get(f"{ENTRIES}/summary", headers=account.headers, params=days)).json()
    assert summary["totals"] == [
        {"currency": "INR", "expense_minor": 1014500, "income_minor": 0, "count": 2}
    ]
    overview = await client.get(
        "/v1/expenses/analytics/summary", headers=account.headers, params=days
    )
    assert overview.json()["spent_minor"] == 1014500
    answer = await client.post(
        "/v1/expenses/query",
        headers=account.headers,
        json={"start": days["from"], "end": days["to"]},
    )
    assert answer.json()["rows"] == [{"key": None, "value": 1014500, "count": 2}]


@pytest.mark.usefixtures("fx_rates")
async def test_totals_never_move_when_rates_do(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = await register(client)
    made = await hotel(client, account, date="2026-09-12")
    monkeypatch.setitem(RATES["USD"], "INR", 90.0)  # the rate moves; the next day is fetched anew
    later = await hotel(client, account, title="Taxi", date="2026-09-13")
    edited = await save(client, account, made, title="Hotel stay")

    assert later["fx_rate"] == "90.0"
    assert edited.status_code == 200, edited.text
    assert (edited.json()["fx_rate"], edited.json()["amount_base_minor"]) == ("83.5", 1002000)


@pytest.mark.usefixtures("fx_rates")
async def test_an_edit_keeps_the_currency_it_was_made_in(client: AsyncClient) -> None:
    account = await register(client)
    made = await hotel(client, account)
    moved = await save(client, account, made, currency="EUR")
    assert (moved.status_code, moved.json()["code"]) == (422, "EXPENSE_CURRENCY_UNSUPPORTED")


async def test_a_currency_with_no_rate_saves_nothing(
    client: AsyncClient, fx_rates: respx.Route
) -> None:
    account = await register(client)
    unknown = await post(client, account, currency="XYZ")
    assert (unknown.status_code, unknown.json()["code"]) == (422, "FX_CURRENCY_UNSUPPORTED")

    fx_rates.side_effect = httpx.ConnectError("down")
    away = await post(client, account, currency="EUR", date="2001-01-01")  # nothing kept then
    assert (away.status_code, away.json()["code"]) == (503, "FX_UNAVAILABLE")
    total = (
        await client.get(ENTRIES, headers=account.headers, params={"from": "2000-01-01"})
    ).json()
    assert total["total"] == 0


@pytest.mark.usefixtures("fx_rates")
async def test_an_entry_in_another_currency_counts_in_my_budgets(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await signed_up_at(client, clock, datetime(2037, 10, 8, 6, tzinfo=UTC))
    budget = await client.post(
        "/v1/expenses/budgets",
        headers=account.headers,
        json={"name": "October", "scope": "overall", "period": "month", "limit_minor": 100000},
    )
    assert budget.status_code == 201, budget.text
    await hotel(client, account, amount_minor=2000, date="2037-10-08")  # $20: ₹1,670

    found = (await client.get("/v1/expenses/budgets", headers=account.headers)).json()["items"]
    assert found[0]["status"]["spent_minor"] == 167000
    notices = (await client.get("/v1/notifications", headers=account.headers)).json()["items"]
    assert "expenses.budget_over" in [item["type"] for item in notices]
