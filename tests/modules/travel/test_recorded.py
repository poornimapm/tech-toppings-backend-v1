"""Each write answers the event of the trip's history it recorded (``event_id``), so the page
undoes exactly that one: none when it changed nothing (an older edit is never taken for it), and a
create sent again answers the first one's."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from tests.helpers import Account, register
from tests.modules.travel.conftest import (
    ACTIVITIES,
    BOOKINGS,
    CHECKLIST,
    EXPENSES,
    FIRST_DAY,
    TRIPS,
    add_item,
    add_trip,
    change_item,
    history,
    newest_event,
    trip_body,
    undo,
)

# Each kind of record: its trip's list, its own route, what makes one, and an edit of it.
KINDS: dict[str, tuple[str, str, dict[str, Any], dict[str, Any]]] = {
    "activity": (
        "activities",
        ACTIVITIES,
        {"title": "Beach", "kind": "beach", "day": FIRST_DAY},
        {"title": "Kayak"},
    ),
    "item": ("checklist", CHECKLIST, {"title": "Towel"}, {"title": "Two towels"}),
    "booking": ("bookings", BOOKINGS, {"kind": "hotel", "title": "Stay"}, {"title": "Beach hut"}),
    "expense": (
        "expenses",
        EXPENSES,
        {"kind": "actual", "category": "food", "title": "Lunch", "amount_minor": 500},
        {"amount_minor": 700},
    ),
}


async def listed(
    client: AsyncClient, account: Account, trip_id: str, kind: str
) -> dict[str, dict[str, Any]]:
    """The trip's records of this kind, by id."""
    found = await client.get(f"{TRIPS}/{trip_id}/{KINDS[kind][0]}", headers=account.headers)
    return {row["id"]: row for row in found.json()}


@pytest.mark.parametrize("kind", KINDS)
async def test_each_write_answers_the_event_it_recorded(client: AsyncClient, kind: str) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    many, one, body, edit = KINDS[kind]
    headers = account.headers

    added = await client.post(f"{TRIPS}/{trip['id']}/{many}", headers=headers, json=body)
    assert added.json()["event_id"] == await newest_event(client, account, trip["id"])
    record_id = added.json()["id"]
    url = f"{one}/{record_id}"

    edited = await client.patch(url, headers=headers, json={**edit, "revision_no": 1})
    assert edited.json()["event_id"] == await newest_event(client, account, trip["id"])
    assert (await undo(client, account, edited.json()["event_id"])).status_code == 200
    back = (await listed(client, account, trip["id"], kind))[record_id]
    assert {name: back[name] for name in edit} == {name: body[name] for name in edit}

    removed = await client.delete(url, headers=headers)
    assert removed.json() == {"event_id": await newest_event(client, account, trip["id"])}
    assert record_id not in await listed(client, account, trip["id"], kind)
    assert (await undo(client, account, removed.json()["event_id"])).status_code == 200
    assert record_id in await listed(client, account, trip["id"], kind)


@pytest.mark.parametrize("kind", KINDS)
async def test_an_edit_that_changes_nothing_answers_no_event(
    client: AsyncClient, kind: str
) -> None:
    """So the page has nothing to undo: an earlier edit, still undoable, is never taken for it."""
    account = await register(client)
    trip = await add_trip(client, account)
    many, one, body, edit = KINDS[kind]
    added = await client.post(f"{TRIPS}/{trip['id']}/{many}", headers=account.headers, json=body)
    url = f"{one}/{added.json()['id']}"
    edited = await client.patch(url, headers=account.headers, json={**edit, "revision_no": 1})
    events_before = await history(client, account, trip["id"])

    same = await client.patch(url, headers=account.headers, json={**edit, "revision_no": 2})

    assert (same.status_code, same.json()["event_id"]) == (200, None)
    assert await history(client, account, trip["id"]) == events_before
    assert events_before[0]["id"] == edited.json()["event_id"]


async def test_a_tick_answers_no_event_as_it_records_none(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    item = await add_item(client, account, trip["id"])
    events_before = await history(client, account, trip["id"])

    ticked = await change_item(client, account, item["id"], state="completed")

    assert (ticked.json()["state"], ticked.json()["event_id"]) == ("completed", None)
    assert await history(client, account, trip["id"]) == events_before


@pytest.mark.parametrize("kind", KINDS)
async def test_a_create_sent_again_answers_the_first_ones_event(
    client: AsyncClient, kind: str
) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    many, _, body, _ = KINDS[kind]
    headers = {**account.headers, "Idempotency-Key": f"{kind}-sent-again"}
    url = f"{TRIPS}/{trip['id']}/{many}"

    first = await client.post(url, headers=headers, json=body)
    again = await client.post(url, headers=headers, json=body)

    assert again.headers["Idempotent-Replayed"] == "true"
    assert again.json()["event_id"] == first.json()["event_id"]
    assert first.json()["event_id"] == await newest_event(client, account, trip["id"])


async def test_a_trip_sent_again_answers_its_making(client: AsyncClient) -> None:
    account = await register(client)
    headers = {**account.headers, "Idempotency-Key": "trip-sent-again"}

    first = await client.post(TRIPS, headers=headers, json=trip_body())
    again = await client.post(TRIPS, headers=headers, json=trip_body())

    assert again.headers["Idempotent-Replayed"] == "true"
    making = await newest_event(client, account, first.json()["id"])
    assert (first.json()["event_id"], again.json()["event_id"]) == (making, making)


async def test_a_mode_switch_answers_no_event(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)

    switched = await client.patch(
        f"{TRIPS}/{trip['id']}", headers=account.headers, json={"mode": "basic", "revision_no": 1}
    )

    assert (switched.json()["mode"], switched.json()["event_id"]) == ("basic", None)
