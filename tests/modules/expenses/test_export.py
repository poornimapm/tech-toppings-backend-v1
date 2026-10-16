"""``GET /v1/expenses/export.csv``: the user's entries as a CSV file with the list's filters,
read right by spreadsheets (UTF-8 with a byte-order mark) and safe to open in them."""

from __future__ import annotations

import csv
import io
from typing import Any

import pytest
from httpx import AsyncClient

from app.modules.expenses.features.exports.service import COLUMNS
from app.modules.expenses.listing import listing
from tests.helpers import Account, register
from tests.modules.expenses.conftest import CATEGORIES, add

EXPORT = "/v1/expenses/export.csv"


async def exported(
    client: AsyncClient, account: Account, **query: str
) -> tuple[list[dict[str, str]], Any]:
    response = await client.get(EXPORT, headers=account.headers, params=query)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.text.startswith("﻿")
    rows = list(csv.DictReader(io.StringIO(response.text.removeprefix("﻿"))))
    return rows, response


async def test_writes_every_field_newest_first(client: AsyncClient) -> None:
    account = await register(client)
    pets = (
        await client.post(
            CATEGORIES,
            headers=account.headers,
            json={"name": "Pets", "icon": "🐶", "accent": "teal"},
        )
    ).json()
    await add(client, account, title="Rice", amount_minor=12550, date="2026-10-01")
    await add(
        client,
        account,
        title="Dog food",
        amount_minor=90000,
        category_id=pets["id"],
        date="2026-10-05",
        time="18:30:00",
        quantity=2,
        unit="kg",
        merchant="Pet Shop",
        payment_mode="upi",
        for_whom="Bruno",
        tags=["pets", "monthly"],
        note="the big bag",
    )
    await add(
        client,
        account,
        title="Salary",
        kind="income",
        category_id="salary",
        amount_minor=5000000,
        date="2026-10-03",
    )

    rows, response = await exported(client, account)
    assert list(rows[0]) == list(COLUMNS)
    assert [row["title"] for row in rows] == ["Dog food", "Salary", "Rice"]
    assert rows[0] == {
        "date": "2026-10-05",
        "time": "18:30:00",
        "kind": "expense",
        "title": "Dog food",
        "amount": "900.00",
        "currency": "INR",
        "category": "Pets",  # the user's own category by its name
        "quantity": "2",
        "unit": "kg",
        "merchant": "Pet Shop",
        "payment_mode": "upi",
        "for_whom": "Bruno",
        "tags": "pets;monthly",
        "note": "the big bag",
    }
    assert (rows[2]["amount"], rows[2]["category"], rows[2]["time"]) == ("125.50", "groceries", "")
    assert (rows[1]["kind"], rows[1]["amount"]) == ("income", "50000.00")
    assert 'filename="expenses-' in response.headers["content-disposition"]


async def test_holds_what_the_list_shows(client: AsyncClient) -> None:
    account = await register(client)
    await add(client, account, title="Rice", date="2026-10-01", tags=["home"])
    await add(client, account, title="Bus", category_id="transport", date="2026-10-02")
    old = await add(client, account, title="Old", date="2026-09-01")
    await client.delete(f"/v1/expenses/entries/{old['id']}", headers=account.headers)

    rows, response = await exported(client, account, **{"from": "2026-10-01", "to": "2026-10-31"})
    assert [row["title"] for row in rows] == ["Bus", "Rice"]  # the deleted one is left out
    assert response.headers["content-disposition"] == (
        'attachment; filename="expenses-2026-10-01-to-2026-10-31.csv"'
    )
    assert [row["title"] for row in (await exported(client, account, tag="home"))[0]] == ["Rice"]
    assert [row["title"] for row in (await exported(client, account, deleted="true"))[0]] == ["Old"]


async def test_only_the_users_own_entries(client: AsyncClient) -> None:
    account = await register(client)
    other = await register(client)
    await add(client, other, title="Theirs")
    assert (await exported(client, account))[0] == []


async def test_text_a_spreadsheet_would_run_is_written_as_text(client: AsyncClient) -> None:
    account = await register(client)
    await add(
        client,
        account,
        title="=HYPERLINK(1)",
        merchant="+91 98765",
        note="@everyone",
        for_whom="-Amma",
        tags=["=1+1"],
    )
    row = (await exported(client, account))[0][0]
    assert (row["title"], row["merchant"], row["note"], row["for_whom"], row["tags"]) == (
        "'=HYPERLINK(1)",
        "'+91 98765",
        "'@everyone",
        "'-Amma",
        "'=1+1",
    )


async def test_keeps_tamil_and_hindi(client: AsyncClient) -> None:
    account = await register(client)
    await add(client, account, title="தக்காளி", merchant="सब्ज़ी मंडी")
    row = (await exported(client, account))[0][0]
    assert (row["title"], row["merchant"]) == ("தக்காளி", "सब्ज़ी मंडी")


async def test_streams_long_files_in_batches(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.modules.expenses.features.exports.service.BATCH", 2)
    account = await register(client)
    for n in range(5):
        await add(client, account, title=f"Item {n}", date=f"2026-10-0{n + 1}")
    rows, _ = await exported(client, account)
    assert [row["title"] for row in rows] == [f"Item {n}" for n in reversed(range(5))]


async def test_a_file_too_long_is_refused_before_it_starts(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    short = listing().model_copy(
        update={"export": listing().export.model_copy(update={"max_rows": 1})}
    )
    monkeypatch.setattr("app.modules.expenses.features.exports.service.listing", lambda: short)
    account = await register(client)
    await add(client, account, title="One", date="2026-10-01")
    await add(client, account, title="Two", date="2026-10-02")

    response = await client.get(EXPORT, headers=account.headers)
    assert response.status_code == 422
    assert (response.json()["code"], response.json()["details"]) == (
        "EXPENSE_EXPORT_TOO_LARGE",
        {"max": 1},
    )
    narrower = await client.get(EXPORT, headers=account.headers, params={"from": "2026-10-02"})
    assert narrower.status_code == 200


async def test_needs_a_signed_in_user(client: AsyncClient) -> None:
    assert (await client.get(EXPORT)).status_code == 401
