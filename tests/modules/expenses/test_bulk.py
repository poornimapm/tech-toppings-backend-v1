"""``POST /v1/expenses/entries/bulk-update``: one change to many entries. Each entry changes by
the usual rules (its own revision, budgets), what can't change is skipped with a reason, and
the activity feed notes the change once."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient

from app.modules.expenses.features.entries.service import EntryService
from app.modules.expenses.listing import listing
from app.platform import VersionConflictError
from tests.helpers import Account, FrozenClock, register
from tests.modules.expenses.conftest import ENTRIES, add, history, listed, signed_up_at, titles

BULK = f"{ENTRIES}/bulk-update"


async def bulk(client: AsyncClient, account: Account, **body: Any) -> dict[str, Any]:
    response = await client.post(BULK, headers=account.headers, json=body)
    assert response.status_code == 200, response.text
    return dict(response.json())


async def activity(client: AsyncClient, account: Account) -> list[tuple[str, dict[str, Any]]]:
    feed = (await client.get("/v1/activity", headers=account.headers)).json()["items"]
    return [(item["type"], item["params"]) for item in feed]


async def test_files_entries_under_a_category_each_with_its_own_revision(
    client: AsyncClient,
) -> None:
    account = await register(client)
    rice = await add(client, account, title="Rice")
    dal = await add(client, account, title="Dal")
    salary = await add(
        client, account, title="Salary", kind="income", category_id="salary", amount_minor=5000000
    )

    done = await bulk(
        client,
        account,
        ids=[rice["id"], dal["id"], salary["id"]],
        action="category",
        category_id="food",
    )
    assert done == {
        "changed": [rice["id"], dal["id"]],
        "skipped": [{"id": salary["id"], "reason": "kind"}],  # food is for expenses
    }
    page = await listed(client, account)
    assert {item["title"]: item["category_id"] for item in page["items"]} == {
        "Rice": "food",
        "Dal": "food",
        "Salary": "salary",
    }
    latest = (await history(client, account, rice["id"]))[0]
    assert (latest["revision_no"], latest["source"]) == (2, "bulk")
    assert latest["changes"] == [{"field": "category_id", "old": "groceries", "new": "food"}]

    again = await bulk(client, account, ids=[rice["id"]], action="category", category_id="food")
    assert again == {"changed": [], "skipped": [{"id": rice["id"], "reason": "unchanged"}]}


async def test_adds_and_takes_off_a_tag(client: AsyncClient) -> None:
    account = await register(client)
    rice = await add(client, account, title="Rice", tags=["home"])
    dal = await add(client, account, title="Dal", tags=["goa"])
    full = await add(client, account, title="Full", tags=[f"t{n}" for n in range(10)])

    added = await bulk(
        client, account, ids=[rice["id"], dal["id"], full["id"]], action="add_tag", tag=" Goa "
    )
    assert added["changed"] == [rice["id"]]
    assert added["skipped"] == [
        {"id": dal["id"], "reason": "unchanged"},
        {"id": full["id"], "reason": "tags_full"},  # an entry keeps at most 10 tags
    ]
    assert sorted(titles(await listed(client, account, "&tag=goa"))) == ["Dal", "Rice"]

    removed = await bulk(
        client, account, ids=[rice["id"], dal["id"]], action="remove_tag", tag="GOA"
    )
    assert removed["changed"] == [rice["id"], dal["id"]]
    assert titles(await listed(client, account, "&tag=goa")) == []
    assert (await listed(client, account, "&tag=home"))["total"] == 1  # other tags stay


async def test_deletes_and_brings_back_only_the_users_own_entries(client: AsyncClient) -> None:
    account = await register(client)
    rice = await add(client, account, title="Rice")
    dal = await add(client, account, title="Dal")
    other = await register(client)
    theirs = await add(client, other, title="Theirs")

    ids = [rice["id"], dal["id"], theirs["id"], "not-an-id", rice["id"]]  # repeats count once
    deleted = await bulk(client, account, ids=ids, action="delete")
    assert deleted == {
        "changed": [rice["id"], dal["id"]],
        "skipped": [
            {"id": theirs["id"], "reason": "not_found"},
            {"id": "not-an-id", "reason": "not_found"},
        ],
    }
    assert (await listed(client, account))["total"] == 0
    assert (await listed(client, other))["total"] == 1  # never touched

    back = await bulk(client, account, ids=[rice["id"], dal["id"]], action="undelete")
    assert back["changed"] == [rice["id"], dal["id"]]
    assert sorted(titles(await listed(client, account))) == ["Dal", "Rice"]
    again = await bulk(client, account, ids=[rice["id"]], action="undelete")
    assert again["skipped"] == [{"id": rice["id"], "reason": "unchanged"}]


async def test_the_activity_feed_notes_a_change_once(client: AsyncClient) -> None:
    account = await register(client)
    rice = await add(client, account, title="Rice")
    dal = await add(client, account, title="Dal")

    await bulk(client, account, ids=[rice["id"], dal["id"]], action="add_tag", tag="home")
    await bulk(client, account, ids=[rice["id"]], action="delete")
    await bulk(client, account, ids=[dal["id"], "missing"], action="delete")
    await bulk(client, account, ids=["missing"], action="delete")  # nothing changed: no note

    assert (await activity(client, account))[:3] == [
        ("expenses.entry_deleted", {"module": "expenses", "title": "Dal"}),  # one: as one
        ("expenses.entry_deleted", {"module": "expenses", "title": "Rice"}),
        ("expenses.entries_changed", {"module": "expenses", "count": 2}),
    ]


@pytest.mark.parametrize(
    "body",
    [
        {"action": "category"},  # which category?
        {"action": "add_tag"},
        {"action": "delete", "tag": "goa"},  # takes nothing more
        {"action": "category", "category_id": "food", "tag": "goa"},
        {"action": "rename"},
    ],
)
async def test_an_action_takes_what_it_needs_and_nothing_else(
    client: AsyncClient, body: dict[str, Any]
) -> None:
    account = await register(client)
    rice = await add(client, account)
    response = await client.post(BULK, headers=account.headers, json={"ids": [rice["id"]], **body})
    assert response.status_code == 422, response.text


async def test_takes_at_least_one_and_at_most_the_configured_entries(client: AsyncClient) -> None:
    account = await register(client)
    most = listing().bulk.max_entries
    for ids in ([], [f"e{n}" for n in range(most + 1)]):
        response = await client.post(
            BULK, headers=account.headers, json={"ids": ids, "action": "delete"}
        )
        assert response.status_code == 422, response.text


async def test_an_unknown_category_changes_nothing(client: AsyncClient) -> None:
    account = await register(client)
    rice = await add(client, account)
    response = await client.post(
        BULK,
        headers=account.headers,
        json={"ids": [rice["id"]], "action": "category", "category_id": "no-such"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "EXPENSE_CATEGORY_INVALID"
    assert (await listed(client, account))["items"][0]["revision_no"] == 1


async def test_an_entry_changed_elsewhere_meanwhile_is_skipped(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = await register(client)
    rice = await add(client, account, title="Rice")
    dal = await add(client, account, title="Dal")
    update = EntryService.update

    async def racing(self: EntryService, entry_id: str, *args: Any, **kwargs: Any) -> Any:
        if entry_id == rice["id"]:
            raise VersionConflictError
        return await update(self, entry_id, *args, **kwargs)

    monkeypatch.setattr(EntryService, "update", racing)
    done = await bulk(client, account, ids=[rice["id"], dal["id"]], action="add_tag", tag="home")
    assert done == {"changed": [dal["id"]], "skipped": [{"id": rice["id"], "reason": "conflict"}]}


async def test_moving_entries_into_a_budget_warns_as_saving_them_would(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await signed_up_at(client, clock, datetime(2037, 10, 8, 6, tzinfo=UTC))
    response = await client.post(
        "/v1/expenses/budgets",
        headers=account.headers,
        json={
            "name": "Eating out",
            "scope": "category",
            "category_id": "food",
            "period": "month",
            "limit_minor": 20000,
        },
    )
    assert response.status_code == 201, response.text
    meals = [await add(client, account, title=f"Meal {n}", date="2037-10-08") for n in range(2)]

    await bulk(
        client, account, ids=[meal["id"] for meal in meals], action="category", category_id="food"
    )
    notices = (await client.get("/v1/notifications", headers=account.headers)).json()["items"]
    assert [item["type"] for item in notices if item["type"].startswith("expenses.budget")] == [
        "expenses.budget_over"
    ]
