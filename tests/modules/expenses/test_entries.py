"""``/v1/expenses/entries``: what is stored, what is refused, and how the list is searched."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from tests.helpers import register
from tests.modules.expenses.conftest import ENTRIES, add, entry, listed, post, save, titles


async def test_an_entry_is_stored_as_given_and_read_back(client: AsyncClient) -> None:
    account = await register(client)

    created = await add(
        client,
        account,
        title="  Vegetables  ",
        merchant=" Big Bazaar ",
        payment_mode="upi",
        for_whom="Amma",
        time="09:30:00",
        tags=["Weekly", "food", " FOOD "],
        note="For the week",
        attachments=[{"url": "https://example.com/bill.pdf", "label": "Bill"}],
    )
    read = await client.get(f"{ENTRIES}/{created['id']}", headers=account.headers)

    assert read.json() == created
    assert created | {"id": "", "created_at": "", "updated_at": ""} == {
        "id": "",
        "kind": "expense",
        "title": "Vegetables",
        "amount_minor": 12500,
        "currency": "INR",  # the user's base currency
        "quantity": None,
        "unit": None,
        "category_id": "groceries",
        "date": "2026-10-08",
        "time": "09:30:00",
        "merchant": "Big Bazaar",
        "payment_mode": "upi",
        "for_whom": "Amma",
        "tags": ["weekly", "food"],  # one spelling per tag
        "note": "For the week",
        "attachments": [{"url": "https://example.com/bill.pdf", "label": "Bill"}],
        "source": "manual",
        "recurring_id": None,  # not from a repeating rule
        "revision_no": 1,
        "deleted": False,
        "created_at": "",
        "updated_at": "",
    }


async def test_an_empty_optional_box_means_none(client: AsyncClient) -> None:
    account = await register(client)

    created = await add(client, account, merchant="   ", note="", for_whom=None)

    assert (created["merchant"], created["note"], created["for_whom"]) == (None, None, None)


@pytest.mark.parametrize(
    "changes",
    [
        {"amount_minor": 0},
        {"amount_minor": 12.5},
        {"title": "   "},
        {"date": "08/10/2026"},
        {"tags": [f"tag{n}" for n in range(11)]},
        {"attachments": [{"url": "not a link"}]},
        {"payment_mode": "barter"},
        {"user_id": "000000000000000000000000"},  # ownership is never taken from the body
    ],
)
async def test_invalid_entries_are_refused(client: AsyncClient, changes: dict[str, Any]) -> None:
    account = await register(client)

    response = await post(client, account, **changes)

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert (await listed(client, account))["total"] == 0


async def test_the_category_must_exist_and_fit_the_kind(client: AsyncClient) -> None:
    account = await register(client)

    wrong_kind = await post(client, account, category_id="salary")
    unknown = await post(client, account, category_id="teleport")
    income = await post(client, account, kind="income", category_id="salary", title="Pay")

    assert (wrong_kind.status_code, unknown.status_code, income.status_code) == (422, 422, 201)
    assert wrong_kind.json()["code"] == "EXPENSE_CATEGORY_INVALID"


async def test_entries_are_kept_in_the_base_currency_for_now(client: AsyncClient) -> None:
    account = await register(client)

    foreign = await post(client, account, currency="USD")
    stated = await post(client, account, currency="INR")

    assert foreign.status_code == 422
    assert foreign.json()["code"] == "EXPENSE_CURRENCY_UNSUPPORTED"
    assert stated.status_code == 201


async def test_the_list_filters_sorts_and_pages(client: AsyncClient) -> None:
    account = await register(client)
    await add(client, account, title="Rice", amount_minor=12500, date="2026-10-08", tags=["Home"])
    await add(
        client,
        account,
        title="Bus pass",
        amount_minor=60000,
        date="2026-10-05",
        category_id="transport",
        payment_mode="card",
        for_whom="Ravi",
    )
    await add(
        client,
        account,
        title="Salary",
        amount_minor=5000000,
        date="2026-10-01",
        kind="income",
        category_id="salary",
        merchant="Acme (India)",
    )
    await add(client, account, title="Tea", amount_minor=2000, date="2026-09-30", note="with Ravi")

    async def found(query: str) -> list[str]:
        return titles(await listed(client, account, query))

    assert await found("") == ["Rice", "Bus pass", "Salary", "Tea"]  # newest day first
    assert await found("&sort=amount_minor") == ["Tea", "Rice", "Bus pass", "Salary"]
    assert await found("&kind=income") == ["Salary"]
    assert await found("&from=2026-10-01&to=2026-10-05") == ["Bus pass", "Salary"]
    assert await found("&category_id=transport") == ["Bus pass"]
    assert await found("&tag=HOME") == ["Rice"]
    assert await found("&for_whom=Ravi") == ["Bus pass"]
    assert await found("&payment_mode=card") == ["Bus pass"]
    assert await found("&q=ravi") == ["Tea"]  # in the note; names are a separate filter
    assert await found("&q=(india)") == ["Salary"]  # typed text is never a pattern
    assert await found("&min_amount=10000&max_amount=60000") == ["Rice", "Bus pass"]

    page = await client.get(f"{ENTRIES}?page_size=3&page=2", headers=account.headers)
    bad_sort = await client.get(f"{ENTRIES}?sort=note", headers=account.headers)
    assert (titles(page.json()), page.json()["total"], page.json()["has_next"]) == (
        ["Tea"],
        4,
        False,
    )
    assert bad_sort.status_code == 422


async def test_the_summary_adds_up_what_the_filters_show(client: AsyncClient) -> None:
    account = await register(client)
    await add(client, account, amount_minor=12500, date="2026-10-08")
    await add(client, account, amount_minor=7550, date="2026-10-07")
    await add(client, account, amount_minor=99900, date="2026-09-01")
    await add(client, account, kind="income", category_id="salary", amount_minor=5000000)

    async def summary(query: str = "") -> list[dict[str, Any]]:
        response = await client.get(f"{ENTRIES}/summary{query}", headers=account.headers)
        return list(response.json()["totals"])

    assert await summary() == [
        {"currency": "INR", "expense_minor": 119950, "income_minor": 5000000, "count": 4}
    ]
    assert await summary("?from=2026-10-01&kind=expense") == [
        {"currency": "INR", "expense_minor": 20050, "income_minor": 0, "count": 2}
    ]
    assert await summary("?q=nothing-like-this") == []


async def test_facets_suggest_what_was_typed_before(client: AsyncClient) -> None:
    account = await register(client)
    await add(client, account, tags=["weekly", "home"], for_whom="Amma", merchant="Big Bazaar")
    await add(client, account, tags=["home"], for_whom="Ravi")
    gone = await add(client, account, tags=["mistake"], merchant="Nowhere")
    await client.delete(f"{ENTRIES}/{gone['id']}", headers=account.headers)

    facets = await client.get(f"{ENTRIES}/facets", headers=account.headers)

    assert facets.json() == {
        "tags": ["home", "weekly"],
        "for_whom": ["Amma", "Ravi"],
        "merchants": ["Big Bazaar"],
    }


async def test_a_deleted_entry_leaves_lists_and_totals_but_can_be_found(
    client: AsyncClient,
) -> None:
    account = await register(client)
    kept = await add(client, account, title="Kept")
    gone = await add(client, account, title="Gone")

    deleted = await client.delete(f"{ENTRIES}/{gone['id']}", headers=account.headers)
    again = await client.delete(f"{ENTRIES}/{gone['id']}", headers=account.headers)
    edit = await save(client, account, gone, title="Edited after deleting")
    still_readable = await client.get(f"{ENTRIES}/{gone['id']}", headers=account.headers)
    summary = await client.get(f"{ENTRIES}/summary", headers=account.headers)

    assert (deleted.status_code, again.status_code, edit.status_code) == (204, 404, 404)
    assert titles(await listed(client, account)) == ["Kept"]
    assert titles(await listed(client, account, "&deleted=true")) == ["Gone"]
    assert still_readable.json()["deleted"] is True  # so it can be shown and brought back
    assert summary.json()["totals"][0]["count"] == 1
    assert kept["id"] != gone["id"]


async def test_each_user_sees_only_their_own_entries(
    client: AsyncClient, other_client: AsyncClient
) -> None:
    asha, ravi = await register(client), await register(other_client)
    await add(client, asha, title="Asha's rice", tags=["asha"])
    await add(other_client, ravi, title="Ravi's tea", amount_minor=2000, tags=["ravi"])

    summary = await client.get(f"{ENTRIES}/summary", headers=asha.headers)
    facets = await client.get(f"{ENTRIES}/facets", headers=asha.headers)

    assert titles(await listed(client, asha)) == ["Asha's rice"]
    assert summary.json()["totals"][0]["expense_minor"] == 12500
    assert facets.json()["tags"] == ["asha"]
    assert entry()["title"] == "Rice"  # the shared default is untouched by either
