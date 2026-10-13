"""``/v1/notifications``: the "notify me" promise kept, and an inbox private to each user."""

from __future__ import annotations

from typing import Any

import pytest
from beanie import PydanticObjectId
from httpx import AsyncClient

from app.core.repository import Scope
from app.platform.modules.models import ModuleOverride
from app.platform.notifications.inbox import Inbox
from tests.helpers import Account, FrozenClock, build_settings, register


async def notifications(client: AsyncClient, account: Account, query: str = "") -> dict[str, Any]:
    response = await client.get(f"/v1/notifications{query}", headers=account.headers)
    assert response.status_code == 200, response.text
    return dict(response.json())


def inbox_of(account: Account) -> Inbox:
    scope = Scope.for_user(PydanticObjectId(account.user_id))
    return Inbox(scope, FrozenClock(), build_settings("mongodb://localhost/unused"))


async def launch(key: str) -> None:
    now = FrozenClock().now()
    await ModuleOverride(key=key, status="live", created_at=now, updated_at=now).insert()


@pytest.mark.usefixtures("clean_overrides")
async def test_a_module_i_asked_about_notifies_me_once_when_it_launches(
    client: AsyncClient, other_client: AsyncClient
) -> None:
    account = await register(client)
    bystander = await register(other_client)
    await client.post("/v1/modules/receipts/interest", headers=account.headers)
    before = await notifications(client, account)
    await launch("receipts")

    await client.get("/v1/modules", headers=account.headers)  # any visit delivers it
    await client.get("/v1/modules", headers=account.headers)
    await other_client.get("/v1/modules", headers=bystander.headers)
    mine = await notifications(client, account, "?unread=true")
    module = (await client.get("/v1/modules/receipts", headers=account.headers)).json()

    assert before["total"] == 0
    assert mine["total"] == 1  # once, however often modules are loaded
    assert (mine["items"][0]["type"], mine["items"][0]["params"]) == (
        "module.launched",
        {"module": "receipts"},
    )
    assert mine["items"][0]["read"] is False
    assert module["notify_requested"] is False  # the promise is kept, so the request is done
    assert (await notifications(other_client, bystander))["total"] == 0  # they never asked


async def test_reading_and_removing(client: AsyncClient) -> None:
    account = await register(client)
    inbox = inbox_of(account)
    for module in ("habits", "journal", "meals"):
        await inbox.notify("module.launched", module=module)
    newest, middle, _oldest = (await notifications(client, account))["items"]

    read = await client.post(f"/v1/notifications/{newest['id']}/read", headers=account.headers)
    removed = await client.delete(f"/v1/notifications/{middle['id']}", headers=account.headers)
    unread = await notifications(client, account, "?unread=true&page_size=1")
    everything = await notifications(client, account)
    all_read = await client.post("/v1/notifications/read-all", headers=account.headers)

    assert read.json()["read"] is True
    assert removed.status_code == 204
    assert unread["total"] == 1  # the bell's count: one request, one item
    assert everything["total"] == 2
    assert all_read.status_code == 204
    assert (await notifications(client, account, "?unread=true"))["total"] == 0


@pytest.mark.usefixtures("client")
async def test_a_dedupe_key_means_once_even_after_it_was_removed() -> None:
    account = Account("x@example.com", "unused", "unused", str(PydanticObjectId()))
    inbox = inbox_of(account)

    await inbox.notify("budget.exceeded", module="expenses", dedupe_key="budget:2026-10", amount=5)
    await inbox.notify("budget.exceeded", module="expenses", dedupe_key="budget:2026-10", amount=9)
    (only,) = await inbox_of(account).model.find(inbox.query()).to_list()
    await inbox.soft_delete(only.id or "")
    await inbox.notify("budget.exceeded", module="expenses", dedupe_key="budget:2026-10")

    assert only.params == {"module": "expenses", "amount": 5}
    assert await inbox.model.find(inbox.query()).count() == 0  # not resurrected


async def test_inboxes_are_private(client: AsyncClient, other_client: AsyncClient) -> None:
    account = await register(client)
    other = await register(other_client)
    await inbox_of(account).notify("module.launched", module="habits")

    theirs = await notifications(other_client, other)
    await other_client.post("/v1/notifications/read-all", headers=other.headers)
    mine = await notifications(client, account, "?unread=true")

    assert theirs["total"] == 0
    assert mine["total"] == 1  # someone else's "read all" never touches mine
