"""Categories: the system ones from the data file, and each user's own."""

from __future__ import annotations

from typing import Any

import pytest
import yaml
from httpx import AsyncClient

from app.modules.expenses.features.categories import service
from tests.helpers import Account, register
from tests.modules.expenses.conftest import CATEGORIES, post, save

# The design system's accent colours (frontend: ModuleAccent).
ACCENTS = {"emerald", "rose", "amber", "sky", "indigo", "orange", "violet", "teal", "slate"}
PETS = {"name": "Pets", "icon": "🐶", "accent": "teal"}


async def categories(client: AsyncClient, account: Account) -> list[dict[str, Any]]:
    response = await client.get(CATEGORIES, headers=account.headers)
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


def test_the_data_file_is_well_formed() -> None:
    data = yaml.safe_load(service.DATA.read_text(encoding="utf-8"))
    items = [item for kind in data.values() for item in kind]
    keys = [item["key"] for item in items]

    assert set(data) == {"expense", "income"}
    assert len(keys) == len(set(keys)), "category keys are ids: they must be unique"
    assert {item["accent"] for item in items} <= ACCENTS
    assert all(set(item) == {"key", "icon", "accent"} for item in items)
    # Every kind needs somewhere to file what fits nowhere else.
    assert {"other_expense", "other_income"} <= set(keys)


async def test_everyone_starts_with_the_system_categories(client: AsyncClient) -> None:
    account = await register(client)

    items = await categories(client, account)

    assert all(item["system"] and item["key"] == item["id"] for item in items)
    assert all(item["name"] is None and not item["archived"] for item in items)
    assert items[0] == {
        "id": "groceries",
        "kind": "expense",
        "key": "groceries",
        "name": None,
        "icon": "🛒",
        "accent": "emerald",
        "system": True,
        "archived": False,
    }
    assert {item["kind"] for item in items} == {"expense", "income"}


async def test_a_user_adds_renames_and_archives_their_own(client: AsyncClient) -> None:
    account = await register(client)

    created = await client.post(CATEGORIES, headers=account.headers, json=PETS)
    url = f"{CATEGORIES}/{created.json()['id']}"
    entry = (await post(client, account, category_id=created.json()["id"])).json()
    renamed = await client.patch(url, headers=account.headers, json={"name": "Pet care"})
    archived = await client.delete(url, headers=account.headers)

    mine = [item for item in await categories(client, account) if not item["system"]]
    assert created.status_code == 201
    assert created.json() | {"id": ""} == PETS | {
        "id": "",
        "kind": "expense",
        "key": None,
        "system": False,
        "archived": False,
    }
    assert (renamed.json()["name"], archived.status_code) == ("Pet care", 204)
    # Still listed, so the entry filed under it keeps its name ...
    assert [(item["name"], item["archived"]) for item in mine] == [("Pet care", True)]
    # ... and that entry can still be edited, but nothing new can be filed there.
    assert (await save(client, account, entry, title="Dog food")).status_code == 200
    assert (await post(client, account, category_id=created.json()["id"])).status_code == 422
    assert (await client.patch(url, headers=account.headers, json={"name": "x"})).status_code == 404


async def test_names_are_unique_per_kind_whatever_the_case(client: AsyncClient) -> None:
    account = await register(client)
    first = await client.post(CATEGORIES, headers=account.headers, json=PETS)
    other = await client.post(CATEGORIES, headers=account.headers, json=PETS | {"name": "Hobbies"})

    same = await client.post(CATEGORIES, headers=account.headers, json=PETS | {"name": " pets "})
    as_income = await client.post(
        CATEGORIES, headers=account.headers, json=PETS | {"kind": "income"}
    )
    clash = await client.patch(
        f"{CATEGORIES}/{other.json()['id']}", headers=account.headers, json={"name": "PETS"}
    )
    await client.delete(f"{CATEGORIES}/{first.json()['id']}", headers=account.headers)
    reused = await client.post(CATEGORIES, headers=account.headers, json=PETS)

    assert (same.status_code, clash.status_code) == (409, 409)
    assert same.json()["code"] == "EXPENSE_CATEGORY_EXISTS"
    assert as_income.status_code == 201  # a different kind is a different list
    assert reused.status_code == 201  # an archived category gives its name back


async def test_each_user_has_their_own(client: AsyncClient, other_client: AsyncClient) -> None:
    asha, ravi = await register(client), await register(other_client)
    created = await client.post(CATEGORIES, headers=asha.headers, json=PETS)

    same_name = await other_client.post(CATEGORIES, headers=ravi.headers, json=PETS)
    borrowed = await post(other_client, ravi, category_id=created.json()["id"])

    assert same_name.status_code == 201
    assert borrowed.status_code == 422  # someone else's category is no category
    assert sum(not item["system"] for item in await categories(client, asha)) == 1


async def test_the_number_of_own_categories_is_limited(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "MAX_CUSTOM", 2)
    account = await register(client)

    statuses = [
        (
            await client.post(
                CATEGORIES, headers=account.headers, json=PETS | {"name": f"Own {number}"}
            )
        ).status_code
        for number in range(3)
    ]

    assert statuses == [201, 201, 409]


@pytest.mark.parametrize(
    "changes", [{"accent": "Bright Red"}, {"name": ""}, {"icon": ""}, {"kind": "transfer"}]
)
async def test_invalid_categories_are_refused(client: AsyncClient, changes: dict[str, Any]) -> None:
    account = await register(client)

    response = await client.post(CATEGORIES, headers=account.headers, json=PETS | changes)

    assert response.status_code == 422
