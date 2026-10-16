"""Helpers for the expenses tests: a valid entry, and short calls to the API."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import datetime
from typing import Any

import pytest
from httpx import AsyncClient, Response
from pydantic import SecretStr

from app.core.config import JobsSettings
from app.modules.expenses.features.entries.models import EntryRevision
from tests.helpers import Account, FrozenClock, build_app, build_settings, register, running_client
from tests.platform.agents.conftest import agent_client  # noqa: F401 - a fixture, used by name
from tests.platform.reminders.test_exactly_once import TOKEN

ENTRIES = "/v1/expenses/entries"
CATEGORIES = "/v1/expenses/categories"
KEY_HEADER = "Idempotency-Key"


def entry(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "title": "Rice",
        "amount_minor": 12500,
        "category_id": "groceries",
        "date": "2026-10-08",
    }
    return body | changes


async def post(
    client: AsyncClient, account: Account, key: str | None = None, **changes: Any
) -> Response:
    headers = account.headers | ({KEY_HEADER: key} if key else {})
    return await client.post(ENTRIES, headers=headers, json=entry(**changes))


async def add(client: AsyncClient, account: Account, **changes: Any) -> dict[str, Any]:
    response = await post(client, account, **changes)
    assert response.status_code == 201, response.text
    return dict(response.json())


async def save(
    client: AsyncClient, account: Account, current: dict[str, Any], **changes: Any
) -> Response:
    """Save ``current`` (an entry as the API returned it) with ``changes``, like the form."""
    fields = {name: current[name] for name in entry() if name in current}
    body = fields | {"revision_no": current["revision_no"]} | changes
    return await client.put(f"{ENTRIES}/{current['id']}", headers=account.headers, json=body)


async def history(client: AsyncClient, account: Account, entry_id: str) -> list[dict[str, Any]]:
    response = await client.get(
        f"{ENTRIES}/{entry_id}/revisions?page_size=100", headers=account.headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == len(response.json()["items"])
    return list(response.json()["items"])


async def listed(client: AsyncClient, account: Account, query: str = "") -> dict[str, Any]:
    response = await client.get(f"{ENTRIES}?page_size=100{query}", headers=account.headers)
    assert response.status_code == 200, response.text
    return dict(response.json())


def titles(page: dict[str, Any]) -> list[str]:
    return [item["title"] for item in page["items"]]


def travel(clock: FrozenClock, to: datetime) -> None:
    clock.advance(seconds=(to - clock.now()).total_seconds())


async def signed_up_at(client: AsyncClient, clock: FrozenClock, when: datetime) -> Account:
    """A new account, then the app's clock moved to ``when``. In that order: access tokens are
    signed with the app's clock but checked against the real one."""
    account = await register(client)
    travel(clock, when)
    return account


@pytest.fixture
async def cron_client(own_mongo_uri: str, clock: FrozenClock) -> AsyncIterator[AsyncClient]:
    """The app with the jobs endpoint on, over a database of its own (the cron reads across
    users)."""
    jobs = JobsSettings(_env_file=None, token=SecretStr(TOKEN))
    async with running_client(build_app(build_settings(own_mongo_uri, jobs=jobs), clock)) as http:
        yield http


@pytest.fixture
def crash_before_revision(monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    """Calling the returned function makes the next revision insert fail once, as if the
    process had died right after the entry itself was written."""
    insert = EntryRevision.insert
    armed: list[bool] = []

    async def failing(self: EntryRevision, *args: Any, **kwargs: Any) -> Any:
        if armed:
            armed.clear()
            raise RuntimeError("simulated crash before the revision was written")
        return await insert(self, *args, **kwargs)

    monkeypatch.setattr(EntryRevision, "insert", failing)
    return lambda: armed.append(True)
