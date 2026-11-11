"""Helpers for the Travel tests: a trip and its activities made through the API, and everything
stored for a person, read straight from the database, to prove what a change left alone."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from beanie import PydanticObjectId
from httpx import AsyncClient, Response

from app.modules.travel.budget import PROVIDER_REST
from app.modules.travel.models import (
    ActivityRevision,
    Booking,
    BookingRevision,
    ChecklistItem,
    ChecklistRevision,
    ExpenseRevision,
    Trip,
    TripActivity,
    TripEvent,
    TripExpense,
    TripRevision,
)
from tests.helpers import Account
from tests.platform.agents.conftest import agent_client  # noqa: F401 - a fixture, used by name
from tests.platform.fx.conftest import fx_rates  # noqa: F401 - a fixture, used by name

TRAVEL = "/v1/travel"
TRIPS = f"{TRAVEL}/trips"
ACTIVITIES = f"{TRAVEL}/activities"
CHECKLIST = f"{TRAVEL}/checklist"
BOOKINGS = f"{TRAVEL}/bookings"
EXPENSES = f"{TRAVEL}/expenses"
FIRST_DAY, SECOND_DAY, LAST_DAY = "2030-03-01", "2030-03-02", "2030-03-05"
COLLECTIONS = (
    Trip,
    TripRevision,
    TripActivity,
    ActivityRevision,
    ChecklistItem,
    ChecklistRevision,
    Booking,
    BookingRevision,
    TripExpense,
    ExpenseRevision,
    TripEvent,
)


@pytest.fixture(autouse=True)
def _rates_provider_asked_afresh() -> Iterator[None]:
    """A test that makes the rates provider fail would leave budgets resting from it after, and
    one it answered would leave what it doesn't have known."""
    PROVIDER_REST.reset()
    yield
    PROVIDER_REST.reset()


def trip_body(**fields: Any) -> dict[str, Any]:
    return {
        "title": "Goa",
        "destinations": [{"name": "Goa", "country": "India"}],
        "start_date": FIRST_DAY,
        "end_date": LAST_DAY,
        **fields,
    }


def made(response: Response) -> dict[str, Any]:
    """What a create answered, as the record reads: the event it recorded is said apart."""
    assert response.status_code == 201, response.text
    record = dict(response.json())
    assert isinstance(record.pop("event_id"), str), "a create records its making"
    return record


async def add_trip(client: AsyncClient, account: Account, **fields: Any) -> dict[str, Any]:
    return made(await client.post(TRIPS, headers=account.headers, json=trip_body(**fields)))


async def add_activity(
    client: AsyncClient, account: Account, trip_id: str, **fields: Any
) -> dict[str, Any]:
    body = {"title": "Beach", "kind": "beach", "day": FIRST_DAY, **fields}
    return made(
        await client.post(f"{TRIPS}/{trip_id}/activities", headers=account.headers, json=body)
    )


async def read_activity(client: AsyncClient, account: Account, activity_id: str) -> dict[str, Any]:
    response = await client.get(f"{ACTIVITIES}/{activity_id}", headers=account.headers)
    assert response.status_code == 200, response.text
    return dict(response.json())


async def timeline(client: AsyncClient, account: Account, trip_id: str, day: str) -> list[str]:
    """The titles on a day, in order."""
    response = await client.get(f"{TRIPS}/{trip_id}/activities?day={day}", headers=account.headers)
    assert response.status_code == 200, response.text
    return [item["title"] for item in response.json()]


async def change(client: AsyncClient, account: Account, activity_id: str, **body: Any) -> Response:
    return await client.post(
        f"{ACTIVITIES}/{activity_id}/change", headers=account.headers, json=body
    )


async def add_item(
    client: AsyncClient, account: Account, trip_id: str, **fields: Any
) -> dict[str, Any]:
    body = {"title": "Sunscreen", **fields}
    return made(
        await client.post(f"{TRIPS}/{trip_id}/checklist", headers=account.headers, json=body)
    )


async def items_of(
    client: AsyncClient, account: Account, trip_id: str, query: str = ""
) -> list[dict[str, Any]]:
    url = f"{TRIPS}/{trip_id}/checklist{query}"
    response = await client.get(url, headers=account.headers)
    assert response.status_code == 200, response.text
    return list(response.json())


async def read_item(client: AsyncClient, account: Account, item: dict[str, Any]) -> dict[str, Any]:
    """The item as it stands now, through its trip's list (no route reads one alone)."""
    found = await items_of(client, account, item["trip_id"])
    return next(row for row in found if row["id"] == item["id"])


async def change_item(client: AsyncClient, account: Account, item_id: str, **body: Any) -> Response:
    return await client.patch(f"{CHECKLIST}/{item_id}", headers=account.headers, json=body)


async def suggest(client: AsyncClient, account: Account, trip_id: str, **body: Any) -> Response:
    return await client.post(
        f"{TRIPS}/{trip_id}/checklist/suggest", headers=account.headers, json=body
    )


async def add_booking(
    client: AsyncClient, account: Account, trip_id: str, **fields: Any
) -> dict[str, Any]:
    body = {"kind": "tour", "title": "Island tour", **fields}
    return made(
        await client.post(f"{TRIPS}/{trip_id}/bookings", headers=account.headers, json=body)
    )


async def add_expense(
    client: AsyncClient, account: Account, trip_id: str, **fields: Any
) -> dict[str, Any]:
    body = {"kind": "actual", "category": "food", "title": "Dinner", "amount_minor": 1000, **fields}
    return made(
        await client.post(f"{TRIPS}/{trip_id}/expenses", headers=account.headers, json=body)
    )


async def history(client: AsyncClient, account: Account, trip_id: str) -> list[dict[str, Any]]:
    """The trip's events, newest first."""
    response = await client.get(f"{TRIPS}/{trip_id}/events", headers=account.headers)
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


async def undo(client: AsyncClient, account: Account, event_id: str) -> Response:
    return await client.post(f"{TRAVEL}/events/{event_id}/undo", headers=account.headers)


async def newest_event(client: AsyncClient, account: Account, trip_id: str) -> str:
    """The id of the trip's latest event."""
    return str((await history(client, account, trip_id))[0]["id"])


def record_of(response: Response) -> dict[str, Any]:
    """What a write answered, as the record reads: without the event it recorded."""
    assert response.status_code == 200, response.text
    return {name: value for name, value in response.json().items() if name != "event_id"}


async def assist(client: AsyncClient, account: Account, trip_id: str, **body: Any) -> str:
    """Ask for AI help on a trip: the run's id, answered at once (202)."""
    response = await client.post(f"{TRIPS}/{trip_id}/assist", headers=account.headers, json=body)
    assert response.status_code == 202, response.text
    return str(response.json()["run_id"])


async def finished(client: AsyncClient, account: Account, run_id: str) -> dict[str, Any]:
    """What the page does: follow the run until it is no longer running."""
    for _ in range(500):
        response = await client.get(f"/v1/agents/runs/{run_id}", headers=account.headers)
        assert response.status_code == 200, response.text
        run: dict[str, Any] = response.json()
        if run["status"] != "running":
            return run
        await asyncio.sleep(0.01)
    raise AssertionError(f"the run never finished: {run_id}")


async def stored(account: Account) -> dict[str, list[dict[str, Any]]]:
    """Every record of this person's Travel, as stored."""
    owner = {"user_id": PydanticObjectId(account.user_id)}
    return {
        document.get_collection_name(): await document.get_pymongo_collection()
        .find(owner)
        .sort("_id", 1)
        .to_list()
        for document in COLLECTIONS
    }
