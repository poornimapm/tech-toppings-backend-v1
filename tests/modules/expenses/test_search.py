"""What the command palette finds in expenses: saved views, loans, products and entries, each
leading to its own place in the module, with a fair share of the hits for each kind."""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient

from tests.helpers import Account, register
from tests.modules.expenses.conftest import add


async def expenses_hits(client: AsyncClient, account: Account, q: str) -> list[dict[str, Any]]:
    response = await client.get("/v1/search", headers=account.headers, params={"q": q})
    assert response.status_code == 200, response.text
    groups = {group["owner"]: group["hits"] for group in response.json()["groups"]}
    return list(groups.get("expenses", []))


async def test_finds_views_loans_products_and_entries(client: AsyncClient) -> None:
    account = await register(client)
    headers = account.headers
    view = (
        await client.post(
            "/v1/expenses/saved-filters",
            headers=headers,
            json={"name": "Goa trip", "view": {"tag": "goa"}},
        )
    ).json()
    loan = (
        await client.post(
            "/v1/expenses/ious",
            headers=headers,
            json={
                "counterparty": "Goa Ravi",
                "direction": "lent",
                "amount_minor": 50000,
                "due_on": "2099-01-31",
            },
        )
    ).json()
    product = (
        await client.post("/v1/expenses/products", headers=headers, json={"title": "Goa cashews"})
    ).json()
    entry = await add(client, account, title="Hotel", merchant="Goa Inn", date="2026-10-03")

    hits = await expenses_hits(client, account, "goa")
    assert [(hit["kind"], hit["title"], hit["path"]) for hit in hits] == [
        ("saved_filter", "Goa trip", f"?view={view['id']}"),
        ("loan_lent", "Goa Ravi", f"plan?show=loans&focus={loan['id']}"),
        ("product", "Goa cashews", f"prices/{product['id']}"),
        ("entry_expense", "Hotel", f"entries/{entry['id']}"),  # found by its place
    ]
    assert (hits[1]["amount_minor"], hits[1]["date"]) == (50000, "2099-01-31")  # still owed
    assert (hits[3]["amount_minor"], hits[3]["currency"], hits[3]["date"]) == (
        12500,
        "INR",
        "2026-10-03",
    )


async def test_many_entries_never_hide_the_one_view(client: AsyncClient) -> None:
    account = await register(client)
    for n in range(12):
        await add(client, account, title=f"Tea {n}")
    await client.post(
        "/v1/expenses/saved-filters",
        headers=account.headers,
        json={"name": "Tea and snacks", "view": {"q": "tea"}},
    )
    hits = await expenses_hits(client, account, "tea")
    assert hits[0]["kind"] == "saved_filter"
    assert len(hits) == 8  # SEARCH_MAX_HITS


async def test_finds_only_the_users_own(client: AsyncClient) -> None:
    account = await register(client)
    other = await register(client)
    await add(client, other, title="Saffron")
    assert await expenses_hits(client, account, "saffron") == []


async def test_reads_what_is_typed_as_text_not_a_pattern(client: AsyncClient) -> None:
    account = await register(client)
    await add(client, account, title="Rice (5 kg)")
    await add(client, account, title="Rice 5 kg bag")
    assert [hit["title"] for hit in await expenses_hits(client, account, "(5 kg")] == [
        "Rice (5 kg)"
    ]
    assert await expenses_hits(client, account, ".*") == []
