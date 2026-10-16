"""``/v1/expenses/saved-filters``: named views of the entries list. A view keeps a period by
name (so "this month" stays this month) or the days a chart chose, and only filters the list
knows; names are the user's own, one view per name."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from app.modules.expenses.listing import listing
from tests.helpers import Account, register

VIEWS = "/v1/expenses/saved-filters"


async def create(
    client: AsyncClient, account: Account, name: str = "Groceries", **view: Any
) -> dict[str, Any]:
    response = await client.post(
        VIEWS, headers=account.headers, json={"name": name, "view": view or {"show": "expense"}}
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def views(client: AsyncClient, account: Account) -> list[dict[str, Any]]:
    response = await client.get(VIEWS, headers=account.headers)
    assert response.status_code == 200, response.text
    return list(response.json())


async def test_a_view_keeps_the_filters_and_a_period_by_name(client: AsyncClient) -> None:
    account = await register(client)
    saved = await create(
        client,
        account,
        "Goa trip food",
        show="expense",
        period="last_month",
        q="  dosa ",
        category_id="dining",
        tag="Goa",
        for_whom="Amma",
        merchant="Saravana Bhavan",
    )
    assert saved["view"] == {
        "show": "expense",
        "period": "last_month",
        "from": None,
        "to": None,
        "q": "dosa",
        "category_id": "dining",
        "tag": "goa",  # as entries keep their tags
        "for_whom": "Amma",
        "merchant": "Saravana Bhavan",
    }
    assert await views(client, account) == [saved]


async def test_a_view_with_nothing_chosen_is_this_month(client: AsyncClient) -> None:
    account = await register(client)
    saved = await create(client, account, "Everything", show="all")
    assert (saved["view"]["show"], saved["view"]["period"]) == ("all", "this_month")


async def test_days_a_chart_chose_are_kept_as_days(client: AsyncClient) -> None:
    account = await register(client)
    saved = await create(client, account, "Diwali", **{"from": "2026-10-18", "to": "2026-10-22"})
    assert (saved["view"]["period"], saved["view"]["from"], saved["view"]["to"]) == (
        None,
        "2026-10-18",
        "2026-10-22",
    )


@pytest.mark.parametrize(
    "view",
    [
        {"period": "this_month", "from": "2026-10-01"},  # a period or days, not both
        {"from": "2026-10-22", "to": "2026-10-18"},  # backwards
        {"period": "next_year"},
        {"show": "pending"},
        {"amount_minor": 100},  # not a filter of the list
        {"q": "x" * 81},
    ],
)
async def test_only_what_the_list_can_show_is_kept(
    client: AsyncClient, view: dict[str, Any]
) -> None:
    account = await register(client)
    response = await client.post(VIEWS, headers=account.headers, json={"name": "Odd", "view": view})
    assert response.status_code == 422, response.text


async def test_views_are_listed_by_name_and_names_are_one_per_user(client: AsyncClient) -> None:
    account = await register(client)
    for name in ("travel", "Bills", "amma"):
        await create(client, account, name)
    assert [item["name"] for item in await views(client, account)] == ["amma", "Bills", "travel"]

    taken = await client.post(
        VIEWS, headers=account.headers, json={"name": " BILLS ", "view": {"show": "income"}}
    )
    assert taken.status_code == 409
    assert taken.json()["code"] == "EXPENSE_SAVED_FILTER_EXISTS"

    # Another user's names are their own.
    other = await register(client)
    await create(client, other, "Bills")


async def test_a_view_is_renamed_or_takes_the_list_as_it_is_now(client: AsyncClient) -> None:
    account = await register(client)
    saved = await create(client, account, "Food", show="expense", category_id="groceries")
    await create(client, account, "Fuel")

    renamed = await client.patch(
        f"{VIEWS}/{saved['id']}", headers=account.headers, json={"name": "Food at home"}
    )
    assert renamed.status_code == 200, renamed.text
    assert (renamed.json()["name"], renamed.json()["view"]["category_id"]) == (
        "Food at home",
        "groceries",
    )

    kept = await client.patch(
        f"{VIEWS}/{saved['id']}", headers=account.headers, json={"view": {"period": "all_time"}}
    )
    assert kept.json()["view"]["period"] == "all_time"
    assert kept.json()["view"]["category_id"] is None  # the whole view is replaced
    assert kept.json()["name"] == "Food at home"

    clash = await client.patch(
        f"{VIEWS}/{saved['id']}", headers=account.headers, json={"name": "fuel"}
    )
    assert clash.status_code == 409


async def test_a_deleted_view_frees_its_name(client: AsyncClient) -> None:
    account = await register(client)
    saved = await create(client, account, "Trips")
    deleted = await client.delete(f"{VIEWS}/{saved['id']}", headers=account.headers)
    assert deleted.status_code == 204
    assert await views(client, account) == []
    again = await client.delete(f"{VIEWS}/{saved['id']}", headers=account.headers)
    assert again.status_code == 404
    await create(client, account, "Trips")


async def test_a_user_keeps_at_most_the_configured_number_of_views(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    few = listing().model_copy(
        update={"saved_filters": listing().saved_filters.model_copy(update={"max_per_user": 2})}
    )
    monkeypatch.setattr("app.modules.expenses.features.saved_filters.service.listing", lambda: few)
    account = await register(client)
    first = await create(client, account, "One")
    await create(client, account, "Two")
    full = await client.post(
        VIEWS, headers=account.headers, json={"name": "Three", "view": {"show": "all"}}
    )
    assert full.status_code == 409
    assert full.json()["code"] == "EXPENSE_SAVED_FILTER_LIMIT"
    assert full.json()["details"] == {"max": 2}

    await client.delete(f"{VIEWS}/{first['id']}", headers=account.headers)
    await create(client, account, "Three")  # deleting one makes room


def test_the_limit_comes_from_the_data_file() -> None:
    assert listing().saved_filters.max_per_user >= 1
