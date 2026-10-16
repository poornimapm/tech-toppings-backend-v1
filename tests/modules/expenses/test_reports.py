"""The monthly report (``/v1/expenses/reports/monthly`` and ``.csv``): a month against the one
before, where the money went, the biggest expenses, and the same figures as a CSV file."""

from __future__ import annotations

import csv
import io

import pytest
from httpx import AsyncClient

from tests.helpers import Account, register
from tests.modules.expenses.conftest import add

REPORT = "/v1/expenses/reports/monthly"


async def september(client: AsyncClient) -> Account:
    """September 2026, and a little of August to compare with."""
    account = await register(client)
    await add(client, account, title="Rice", amount_minor=100000, date="2026-09-05")
    await add(client, account, title="Dal", amount_minor=50000, date="2026-09-20", for_whom="Amma")
    await add(
        client,
        account,
        title="Metro",
        amount_minor=30000,
        category_id="transport",
        merchant="Metro",
        date="2026-09-10",
    )
    await add(
        client,
        account,
        title="Salary",
        kind="income",
        category_id="salary",
        amount_minor=5000000,
        date="2026-09-01",
    )
    await add(client, account, title="Rice", amount_minor=100000, date="2026-08-10")
    return account


async def test_sums_up_a_month_against_the_one_before(client: AsyncClient) -> None:
    account = await september(client)
    response = await client.get(REPORT, headers=account.headers, params={"month": "2026-09"})
    assert response.status_code == 200, response.text
    report = response.json()

    summary = report["summary"]
    assert (summary["start"], summary["end"]) == ("2026-09-01", "2026-09-30")
    assert (summary["spent_minor"], summary["received_minor"], summary["net_minor"]) == (
        180000,
        5000000,
        4820000,
    )
    assert summary["previous_totals"]["spent_minor"] == 100000
    assert [
        (item["category_id"], item["amount_minor"], item["previous_minor"], item["change_pct"])
        for item in report["categories"]
    ] == [("groceries", 150000, 100000, 50.0), ("transport", 30000, 0, None)]
    assert [(item["category_id"], item["amount_minor"]) for item in report["income"]] == [
        ("salary", 5000000)
    ]
    assert [item["merchant"] for item in report["merchants"]] == ["Metro"]
    assert "Amma" in [item["name"] for item in report["people"]]
    assert len(report["days"]) == 30
    assert next(day for day in report["days"] if day["date"] == "2026-09-05")["amount_minor"] == (
        100000
    )
    assert [item["amount_minor"] for item in report["largest"]] == [100000, 50000, 30000]


async def test_this_month_counts_up_to_today(client: AsyncClient) -> None:
    account = await register(client)
    response = await client.get(REPORT, headers=account.headers)
    assert response.status_code == 200, response.text
    summary = response.json()["summary"]
    assert summary["start"].endswith("-01")
    assert response.json()["month"] == summary["start"][:7]


async def test_the_csv_holds_the_same_figures(client: AsyncClient) -> None:
    account = await september(client)
    response = await client.get(
        f"{REPORT}.csv", headers=account.headers, params={"month": "2026-09"}
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-disposition"] == (
        'attachment; filename="expenses-report-2026-09.csv"'
    )
    assert response.text.startswith("﻿")
    rows = list(csv.reader(io.StringIO(response.text.removeprefix("﻿"))))
    assert rows[0] == ["section", "name", "amount", "share_pct", "previous", "change_pct"]
    assert rows[1] == ["total", "spent", "1800.00", "", "1000.00", "80"]
    assert ["category", "groceries", "1500.00", "83.3", "1000.00", "50"] in rows
    assert ["income", "salary", "50000.00", "100", "", ""] in rows
    assert ["place", "Metro", "300.00", "16.7", "", ""] in rows


@pytest.mark.parametrize("month", ["2026-13", "2026-9", "september"])
async def test_a_month_is_written_year_dash_month(client: AsyncClient, month: str) -> None:
    account = await register(client)
    response = await client.get(REPORT, headers=account.headers, params={"month": month})
    assert response.status_code == 422


async def test_only_the_users_own_entries(client: AsyncClient) -> None:
    await september(client)
    other = await register(client)
    report = (await client.get(REPORT, headers=other.headers, params={"month": "2026-09"})).json()
    assert (report["summary"]["spent_minor"], report["largest"]) == (0, [])
