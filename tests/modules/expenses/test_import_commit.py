"""``POST /v1/expenses/import/commit``: a CSV file's importable rows added in batches, each row
once per file (its client key), as entries made by the usual rules."""

from __future__ import annotations

import base64
from typing import Any

import pytest
from httpx import AsyncClient

from app.modules.expenses.features.imports.rules import import_rules
from tests.helpers import Account, register
from tests.modules.expenses.conftest import add, history, listed, titles

COMMIT = "/v1/expenses/import/commit"
MAPPING = {
    "header_row": 0,
    "columns": {"date": 0, "title": 1, "amount": 2},
    "date_format": "%Y-%m-%d",
}
FILE = (
    "date,title,amount\n"
    "2026-10-01,Tea,20\n"
    "2026-10-01,Tea,20\n"  # two teas the same day are two teas
    "2026-10-02,Rice,125.50\n"
    "2026-10-03,,10\n"  # no title: never added
    "2026-10-04,Bus,30\n"
)


def encoded(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


async def commit(
    client: AsyncClient, account: Account, text: str = FILE, **options: Any
) -> dict[str, Any]:
    body = {"file": encoded(text), "mapping": MAPPING} | options
    response = await client.post(COMMIT, headers=account.headers, json=body)
    assert response.status_code == 200, response.text
    return dict(response.json())


async def imported_notes(client: AsyncClient, account: Account) -> list[dict[str, Any]]:
    feed = (await client.get("/v1/activity", headers=account.headers)).json()["items"]
    return [item["params"] for item in feed if item["type"] == "expenses.entries_imported"]


async def test_adds_the_rows_that_can_be_entries(client: AsyncClient) -> None:
    account = await register(client)
    done = await commit(client, account)
    assert done == {"added": 4, "already": 0, "done": 4, "total": 4, "next_offset": None}
    page = await listed(client, account)
    assert sorted(titles(page)) == ["Bus", "Rice", "Tea", "Tea"]
    rice = next(item for item in page["items"] if item["title"] == "Rice")
    assert (rice["amount_minor"], rice["source"], rice["category_id"]) == (
        12550,
        "import",
        "other_expense",
    )
    assert (await history(client, account, rice["id"]))[0]["source"] == "import"
    assert await imported_notes(client, account) == [{"module": "expenses", "count": 4}]


async def test_the_same_file_adds_nothing_twice(client: AsyncClient) -> None:
    account = await register(client)
    await commit(client, account)
    again = await commit(client, account)
    assert (again["added"], again["already"]) == (0, 4)
    assert (await listed(client, account))["total"] == 4
    assert len(await imported_notes(client, account)) == 1  # nothing new to note


async def test_adds_in_batches_each_asked_for_in_turn(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    rules = import_rules()
    small = rules.model_copy(update={"limits": rules.limits.model_copy(update={"commit_batch": 1})})
    monkeypatch.setattr("app.modules.expenses.features.imports.service.import_rules", lambda: small)
    account = await register(client)

    offsets: list[int | None] = [0]
    while offsets[-1] is not None:
        step = await commit(client, account, offset=offsets[-1])
        assert (step["added"], step["total"]) == (1, 4)
        offsets.append(step["next_offset"])
    assert offsets == [0, 1, 2, 3, None]
    # Both teas: the first added doesn't make the second a duplicate of this same file.
    assert sorted(titles(await listed(client, account))) == ["Bus", "Rice", "Tea", "Tea"]
    assert await imported_notes(client, account) == [{"module": "expenses", "count": 4}]

    resent = await commit(client, account, offset=2)  # a batch whose reply was lost
    assert (resent["added"], resent["already"]) == (0, 1)


async def test_leaves_out_what_is_saved_already_unless_asked(client: AsyncClient) -> None:
    account = await register(client)
    await add(client, account, title="Rice", amount_minor=12550, date="2026-10-02")

    done = await commit(client, account)
    assert (done["added"], done["total"]) == (3, 3)
    assert titles(await listed(client, account, "&q=rice")) == ["Rice"]

    also = await commit(client, account, include_duplicates=True)
    assert (also["added"], also["already"], also["total"]) == (1, 3, 4)
    assert titles(await listed(client, account, "&q=rice")) == ["Rice", "Rice"]


async def test_needs_the_mapping_the_preview_showed(client: AsyncClient) -> None:
    account = await register(client)
    response = await client.post(COMMIT, headers=account.headers, json={"file": encoded(FILE)})
    assert response.status_code == 422


async def test_imports_only_into_the_users_own_entries(client: AsyncClient) -> None:
    account = await register(client)
    other = await register(client)
    await commit(client, account)
    assert (await listed(client, other))["total"] == 0
    # The same file is another user's own import: all of it is theirs to add.
    assert (await commit(client, other))["added"] == 4
