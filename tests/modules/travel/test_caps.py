"""Each kind's cap (data/travel.yaml) holds on every write that makes or brings back a record, an
undo's and a trip's undelete too, so a list read whole never leaves one out. A create sent again
with its Idempotency-Key answers what the first one made, cap or not."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from app.modules.travel.rules import rules
from app.platform import REPLAYED_HEADER
from tests.helpers import register
from tests.modules.travel.conftest import (
    EXPENSES,
    FIRST_DAY,
    TRIPS,
    add_expense,
    add_trip,
    history,
    trip_body,
    undo,
)


def capped(monkeypatch: pytest.MonkeyPatch, part: str, **limit: int) -> None:
    """One cap of data/travel.yaml lowered, where the stores hold it."""
    lowered = getattr(rules(), part).model_copy(update=limit)
    monkeypatch.setattr(
        "app.modules.travel.events.rules", lambda: rules().model_copy(update={part: lowered})
    )


async def test_an_undo_brings_nothing_back_past_the_cap(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    capped(monkeypatch, "budget", max_expenses_per_trip=2)
    first = await add_expense(client, account, trip["id"])
    await add_expense(client, account, trip["id"])
    await client.delete(f"{EXPENSES}/{first['id']}", headers=account.headers)
    removal = (await history(client, account, trip["id"]))[0]
    await add_expense(client, account, trip["id"])

    refused = await undo(client, account, removal["id"])

    assert (refused.status_code, refused.json()["code"]) == (409, "TRAVEL_EXPENSES_FULL")
    listed = await client.get(f"{TRIPS}/{trip['id']}/expenses", headers=account.headers)
    budget = await client.get(f"{TRIPS}/{trip['id']}/budget", headers=account.headers)
    assert (len(listed.json()), budget.json()["actual_minor"]) == (2, 2000)
    events = {item["id"]: item for item in await history(client, account, trip["id"])}
    assert events[removal["id"]]["undoable"]  # still there to be tried once there is room


async def test_a_deleted_trip_comes_back_only_while_there_is_room_for_it(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = await register(client)
    capped(monkeypatch, "trips", max_per_user=1)
    first = await add_trip(client, account)
    await client.delete(f"{TRIPS}/{first['id']}", headers=account.headers)
    await add_trip(client, account, title="Second")

    refused = await client.post(f"{TRIPS}/{first['id']}/undelete", headers=account.headers)

    assert (refused.status_code, refused.json()["code"]) == (409, "TRAVEL_TRIPS_FULL")
    listed = (await client.get(TRIPS, headers=account.headers)).json()["items"]
    assert [trip["title"] for trip in listed] == ["Second"]


async def test_a_trip_sent_again_answers_the_first_even_at_the_cap(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = await register(client)
    capped(monkeypatch, "trips", max_per_user=1)
    headers = {**account.headers, "Idempotency-Key": "trip-at-the-cap"}

    first = await client.post(TRIPS, headers=headers, json=trip_body())
    again = await client.post(TRIPS, headers=headers, json=trip_body())
    another = await client.post(TRIPS, headers=account.headers, json=trip_body())

    assert (first.status_code, again.status_code) == (201, 201), again.text
    assert (again.json()["id"], again.headers[REPLAYED_HEADER]) == (first.json()["id"], "true")
    assert (another.status_code, another.json()["code"]) == (409, "TRAVEL_TRIPS_FULL")


@pytest.mark.parametrize(
    "case",
    [
        (
            ("activities", {"max_per_trip": 1}),
            "activities",
            {"title": "Beach", "kind": "beach", "day": FIRST_DAY},
            "TRAVEL_ACTIVITIES_FULL",
        ),
        (
            ("checklist", {"max_per_trip": 1}),
            "checklist",
            {"title": "Sunscreen"},
            "TRAVEL_CHECKLIST_FULL",
        ),
        (
            ("bookings", {"max_per_trip": 1}),
            "bookings",
            {"kind": "tour", "title": "Island tour"},
            "TRAVEL_BOOKINGS_FULL",
        ),
        (
            ("budget", {"max_expenses_per_trip": 1}),
            "expenses",
            {"kind": "actual", "category": "food", "title": "Dinner", "amount_minor": 1000},
            "TRAVEL_EXPENSES_FULL",
        ),
    ],
    ids=["activity", "item", "booking", "expense"],
)
async def test_a_record_sent_again_answers_the_first_even_at_the_cap(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[tuple[str, dict[str, int]], str, dict[str, Any], str],
) -> None:
    """(the cap lowered, the route, what is sent, what one more answers)"""
    (part, limit), route, body, code = case
    account = await register(client)
    trip = await add_trip(client, account)
    capped(monkeypatch, part, **limit)
    url = f"{TRIPS}/{trip['id']}/{route}"
    headers = {**account.headers, "Idempotency-Key": f"{route}-at-the-cap"}

    first = await client.post(url, headers=headers, json=body)
    again = await client.post(url, headers=headers, json=body)
    another = await client.post(url, headers=account.headers, json=body)

    assert (first.status_code, again.status_code) == (201, 201), again.text
    assert (again.json()["id"], again.headers[REPLAYED_HEADER]) == (first.json()["id"], "true")
    assert (another.status_code, another.json()["code"]) == (409, code)
