"""The activity feed: what the user did, as codes the client translates, private to them."""

from __future__ import annotations

from httpx import AsyncClient

from tests.helpers import Account, register


async def act(client: AsyncClient, account: Account) -> None:
    headers = account.headers
    await client.patch("/v1/modules/journal/preferences", headers=headers, json={"pinned": True})
    await client.patch("/v1/modules/meals/preferences", headers=headers, json={"enabled": False})
    await client.post("/v1/modules/medicine/interest", headers=headers)
    await client.put("/v1/modules/order", headers=headers, json={"keys": ["journal"]})


async def test_records_what_i_did_newest_first(client: AsyncClient) -> None:
    account = await register(client)
    await act(client, account)

    page = (await client.get("/v1/activity", headers=account.headers)).json()

    assert [(item["type"], item["params"]) for item in page["items"]] == [
        ("module.arranged", {}),
        ("module.notify_on", {"module": "medicine"}),
        ("module.hidden", {"module": "meals"}),
        ("module.pinned", {"module": "journal"}),
    ]
    assert page["total"] == 4


async def test_is_paginated_and_private(client: AsyncClient, other_client: AsyncClient) -> None:
    account = await register(client)
    other = await register(other_client)
    await act(client, account)

    first = (await client.get("/v1/activity?page_size=3", headers=account.headers)).json()
    theirs = (await other_client.get("/v1/activity", headers=other.headers)).json()

    assert (len(first["items"]), first["has_next"]) == (3, True)
    assert theirs["items"] == []


async def test_changes_made_through_the_assistant_are_recorded_too(client: AsyncClient) -> None:
    account = await register(client)
    run = await client.post(
        "/v1/agents/assistant/runs", headers=account.headers, json={"input": "pin habits"}
    )
    proposal = run.json()["proposals"][0]["id"]
    before = (await client.get("/v1/activity", headers=account.headers)).json()["total"]

    await client.post(f"/v1/agents/proposals/{proposal}/confirm", headers=account.headers)
    after = (await client.get("/v1/activity", headers=account.headers)).json()

    assert before == 0  # a proposal alone changes nothing
    assert after["items"][0]["type"] == "module.pinned"
    assert after["items"][0]["params"] == {"module": "habits"}


async def test_feeds_the_dashboard(client: AsyncClient) -> None:
    account = await register(client)
    await act(client, account)

    body = (await client.get("/v1/dashboard", headers=account.headers)).json()
    widgets = {item["key"]: item["data"] for item in body["widgets"]}

    assert (widgets["activity_actions"]["value"], widgets["activity_actions"]["previous"]) == (4, 0)
    recent = widgets["activity_recent"]["items"]
    assert recent[0]["code"] == "module.arranged"
    assert recent[-1] == {
        "title": None, "code": "module.pinned", "params": {"module": "journal"},
        "at": recent[-1]["at"],
    }  # fmt: skip
