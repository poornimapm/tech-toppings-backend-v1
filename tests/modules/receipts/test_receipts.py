"""Receipts & warranty: receipts round trip with their checks, where each warranty stands
today, a reminder before each one ends, and finding them (filters, search, the dashboard). One
user never sees another's."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from beanie import PydanticObjectId
from fastapi import FastAPI
from httpx import AsyncClient

from app.modules.receipts.warranty import months_after, rules
from app.modules.receipts.widgets import receipts_warranties_ending
from app.platform import DateRange, Reminder
from tests.helpers import Account, actor_of, register

ITEMS = "/v1/receipts/items"


async def today_of(app: FastAPI, account: Account) -> date:
    return (await actor_of(app, account)).today()


async def add(client: AsyncClient, account: Account, **fields: Any) -> dict[str, Any]:
    response = await client.post(ITEMS, headers=account.headers, json={"text": "Fridge", **fields})
    assert response.status_code == 201, response.text
    return dict(response.json())


async def reminders_of(account: Account) -> list[Reminder]:
    owner = PydanticObjectId(account.user_id)
    return await Reminder.find({"user_id": owner, "deleted_at": None}).to_list()


async def test_a_receipt_is_kept_read_changed_and_deleted(
    client: AsyncClient, app: FastAPI
) -> None:
    account = await register(client)
    today = await today_of(app, account)
    bought = today - timedelta(days=10)
    ends = months_after(bought, 12)

    item = await add(
        client,
        account,
        text="  Samsung 253L Refrigerator  ",
        shop="Vijay Sales",
        bought_on=bought.isoformat(),
        amount_minor=2_849_000,
        bill_number="INV/26/0042",
        serial_number="RT28SN0001",
        warranty_ends_on=ends.isoformat(),
        remind_days_before=15,
        note="  ",
    )
    url = f"{ITEMS}/{item['id']}"

    assert item["text"] == "Samsung 253L Refrigerator"  # spaces around it are dropped
    assert (item["shop"], item["note"]) == ("Vijay Sales", None)  # an emptied field is no value
    assert (item["amount_minor"], item["currency"]) == (2_849_000, "INR")
    assert (item["warranty_ends_on"], item["warranty"]) == (ends.isoformat(), "active")
    assert (await client.get(url, headers=account.headers)).json() == item

    changed = await client.patch(
        url, headers=account.headers, json={"text": "Fridge", "bought_on": bought.isoformat()}
    )
    assert changed.status_code == 200, changed.text
    # A change replaces the record: no warranty sent is no warranty, and its reminder goes.
    assert (changed.json()["text"], changed.json()["warranty"]) == ("Fridge", "none")
    assert changed.json()["amount_minor"] is None
    assert await reminders_of(account) == []

    assert (await client.delete(url, headers=account.headers)).status_code == 204
    assert (await client.get(url, headers=account.headers)).status_code == 404
    assert (await client.get(ITEMS, headers=account.headers)).json()["total"] == 0


async def test_what_is_left_out_has_a_default(client: AsyncClient, app: FastAPI) -> None:
    account = await register(client)
    today = await today_of(app, account)

    item = await add(client, account, warranty_ends_on=months_after(today, 6).isoformat())

    assert item["bought_on"] == today.isoformat()
    assert item["remind_days_before"] == rules().warranty.remind_days_before
    assert item["currency"] == "INR"  # the user's base currency


@pytest.mark.parametrize(
    "fields",
    [
        {"text": ""},
        {"text": "   "},
        {"text": "x" * 201},
        {"text": "Fridge", "shop": "x" * 81},
        {"text": "Fridge", "amount_minor": 0},
        {"text": "Fridge", "remind_days_before": -1},
        {"text": "Fridge", "remind_days_before": rules().warranty.max_remind_days + 1},
        {"text": "Fridge", "colour": "red"},
    ],
)
async def test_what_the_form_sends_is_checked(client: AsyncClient, fields: dict[str, Any]) -> None:
    account = await register(client)

    response = await client.post(ITEMS, headers=account.headers, json=fields)

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_a_receipt_is_never_dated_after_today(client: AsyncClient, app: FastAPI) -> None:
    account = await register(client)
    tomorrow = await today_of(app, account) + timedelta(days=1)

    response = await client.post(
        ITEMS, headers=account.headers, json={"text": "Fridge", "bought_on": tomorrow.isoformat()}
    )

    assert (response.status_code, response.json()["code"]) == (422, "RECEIPT_DATE_INVALID")


@pytest.mark.parametrize("ends", ["before buying", "beyond the longest"])
async def test_a_warranty_ends_after_buying_and_within_the_limit(
    client: AsyncClient, app: FastAPI, ends: str
) -> None:
    account = await register(client)
    bought = await today_of(app, account) - timedelta(days=30)
    day = (
        bought - timedelta(days=1)
        if ends == "before buying"
        else months_after(bought, rules().warranty.max_months) + timedelta(days=1)
    )

    response = await client.post(
        ITEMS,
        headers=account.headers,
        json={
            "text": "Fridge",
            "bought_on": bought.isoformat(),
            "warranty_ends_on": day.isoformat(),
        },
    )

    assert (response.status_code, response.json()["code"]) == (422, "RECEIPT_WARRANTY_INVALID")


async def test_where_each_warranty_stands_today_and_the_filters(
    client: AsyncClient, app: FastAPI
) -> None:
    account = await register(client)
    today = await today_of(app, account)
    long_ago = today - timedelta(days=800)
    kinds = {
        "none": {"text": "Kettle"},
        "active": {"text": "Fridge", "warranty_ends_on": (today + timedelta(days=200)).isoformat()},
        "ending": {"text": "Phone", "warranty_ends_on": (today + timedelta(days=10)).isoformat()},
        "last day": {"text": "Mixer", "warranty_ends_on": today.isoformat()},
        "ended": {
            "text": "Fan",
            "bought_on": long_ago.isoformat(),
            "warranty_ends_on": (today - timedelta(days=1)).isoformat(),
        },
    }
    for fields in kinds.values():
        await add(client, account, remind_days_before=30, **fields)

    listed = (await client.get(ITEMS, headers=account.headers)).json()["items"]
    found = {
        status: [
            item["text"]
            for item in (
                await client.get(f"{ITEMS}?warranty={status}", headers=account.headers)
            ).json()["items"]
        ]
        for status in ("none", "active", "ending", "ended")
    }

    assert {item["text"]: item["warranty"] for item in listed} == {
        "Kettle": "none",
        "Fridge": "active",
        "Phone": "ending",
        "Mixer": "ending",  # a warranty covers its last day
        "Fan": "ended",
    }
    assert found == {
        "none": ["Kettle"],
        "active": ["Fridge"],
        "ending": ["Mixer", "Phone"],  # latest added first, among those bought the same day
        "ended": ["Fan"],
    }
    assert listed[-1]["text"] == "Fan"  # latest bought first: the fan was bought long ago


async def test_receipts_are_sorted_and_found_by_their_words(client: AsyncClient) -> None:
    account = await register(client)
    await add(client, account, text="Washing machine", shop="Croma", bill_number="CR-77")
    await add(client, account, text="Air cooler", serial_number="SYM-123-XY")
    await add(client, account, text="Mixer", note="kept in the kitchen")

    def texts(response: Any) -> list[str]:
        return [item["text"] for item in response.json()["items"]]

    by_text = await client.get(f"{ITEMS}?sort=text", headers=account.headers)
    unknown = await client.get(f"{ITEMS}?sort=colour", headers=account.headers)
    found = {
        words: texts(await client.get(f"{ITEMS}?q={words}", headers=account.headers))
        for words in ("croma", "cr-77", "sym-123", "KITCHEN", "nothing")
    }

    assert texts(by_text) == ["Air cooler", "Mixer", "Washing machine"]
    assert unknown.status_code == 422
    assert found == {
        "croma": ["Washing machine"],
        "cr-77": ["Washing machine"],
        "sym-123": ["Air cooler"],
        "KITCHEN": ["Mixer"],
        "nothing": [],
    }


async def test_a_reminder_comes_before_each_warranty_ends(
    client: AsyncClient, app: FastAPI
) -> None:
    account = await register(client)
    today = await today_of(app, account)
    ends = today + timedelta(days=100)

    item = await add(client, account, warranty_ends_on=ends.isoformat(), remind_days_before=20)
    [reminder] = await reminders_of(account)
    later = ends + timedelta(days=30)
    await client.patch(
        f"{ITEMS}/{item['id']}",
        headers=account.headers,
        json={
            "text": "Fridge",
            "bought_on": item["bought_on"],
            "warranty_ends_on": later.isoformat(),
            "remind_days_before": 20,
        },
    )
    [moved] = await reminders_of(account)
    await client.delete(f"{ITEMS}/{item['id']}", headers=account.headers)

    assert (reminder.type, reminder.module_key, reminder.ref_id) == (
        "receipts.warranty_ends",
        "receipts",
        item["id"],
    )
    assert reminder.due_on == ends.isoformat()
    assert reminder.params == {"title": "Fridge", "receipt_id": item["id"]}
    # 20 days ahead, at the user's send hour (in UTC, maybe the evening before).
    assert ends - timedelta(days=21) <= reminder.remind_at.date() <= ends - timedelta(days=20)
    assert moved.due_on == later.isoformat()  # one live reminder: the new end replaced it
    assert await reminders_of(account) == []  # deleted: no reminder left


async def test_an_ended_warranty_is_never_reminded(client: AsyncClient, app: FastAPI) -> None:
    account = await register(client)
    today = await today_of(app, account)

    await add(
        client,
        account,
        bought_on=(today - timedelta(days=400)).isoformat(),
        warranty_ends_on=(today - timedelta(days=5)).isoformat(),
    )

    assert await reminders_of(account) == []


async def test_search_and_the_dashboard_find_them(client: AsyncClient, app: FastAPI) -> None:
    account = await register(client)
    today = await today_of(app, account)
    item = await add(
        client,
        account,
        text="Pixel phone",
        amount_minor=4_500_000,
        warranty_ends_on=(today + timedelta(days=5)).isoformat(),
    )
    await add(
        client, account, text="Laptop", warranty_ends_on=(today + timedelta(days=300)).isoformat()
    )

    found = (await client.get("/v1/search?q=pixel", headers=account.headers)).json()
    ctx = await actor_of(app, account)
    week = DateRange(today - timedelta(days=6), today, ctx.user.timezone)
    ending = await receipts_warranties_ending(ctx, week)

    [hit] = [
        hit for group in found["groups"] for hit in group["hits"] if group["owner"] == "receipts"
    ]
    assert (hit["kind"], hit["title"], hit["path"], hit["amount_minor"]) == (
        "receipt",
        "Pixel phone",
        item["id"],
        4_500_000,
    )
    assert [(row.code, row.params["title"]) for row in ending.items] == [
        ("receipts.warranty_ends", "Pixel phone")  # the laptop's isn't ending yet
    ]


async def test_the_form_options_come_from_the_modules_data(client: AsyncClient) -> None:
    account = await register(client)

    options = (await client.get("/v1/receipts/options", headers=account.headers)).json()

    warranty = rules().warranty
    assert options == {
        "remind_days_before": warranty.remind_days_before,
        "max_remind_days": warranty.max_remind_days,
        "max_months": warranty.max_months,
        "quick_months": list(warranty.quick_months),
    }


async def test_the_tile_counts_the_users_own_receipts(
    client: AsyncClient, other_client: AsyncClient
) -> None:
    owner, other = await register(client), await register(other_client)
    for text in ("a", "b"):
        await add(client, owner, text=text)
    await add(other_client, other, text="c")

    view = (await client.get("/v1/modules/receipts", headers=owner.headers)).json()

    assert (view["status"], view["available"]) == ("beta", True)
    assert view["stat"] == {"value": 2, "format": "number", "currency": None, "label": "items"}


async def test_one_user_never_sees_anothers_receipts(
    client: AsyncClient, other_client: AsyncClient
) -> None:
    owner, intruder = await register(client), await register(other_client)
    item = await add(client, owner, text="mine")
    url = f"{ITEMS}/{item['id']}"

    listed = (await other_client.get(ITEMS, headers=intruder.headers)).json()
    found = (await other_client.get("/v1/search?q=mine", headers=intruder.headers)).json()
    read = await other_client.get(url, headers=intruder.headers)
    changed = await other_client.patch(url, headers=intruder.headers, json={"text": "theirs"})
    deleted = await other_client.delete(url, headers=intruder.headers)

    assert listed["total"] == 0
    assert all(group["owner"] != "receipts" for group in found["groups"])
    assert (read.status_code, changed.status_code, deleted.status_code) == (404, 404, 404)
    assert (await client.get(url, headers=owner.headers)).json()["text"] == "mine"
