"""How the route matrix (tests/isolation) makes one of this module's records for a user, and checks
afterwards that nobody else changed it: for every route under ``PREFIX`` that takes an id."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from httpx import AsyncClient

from tests.helpers import Account

PREFIX = "/v1/travel/"
DAY = "2030-03-01"
NOBODY = "000000000000000000000000"
TRIP_BODY = {
    "title": "mine",
    "destinations": [{"name": "Goa", "country": "India"}],
    "start_date": DAY,
    "end_date": "2030-03-05",
}
ACTIVITY_BODY: dict[str, object] = {"title": "mine", "kind": "beach", "day": DAY}
ITEM_BODY: dict[str, object] = {"title": "mine", "day": DAY}
BOOKING_BODY: dict[str, object] = {"kind": "hotel", "title": "mine"}
EXPENSE_BODY: dict[str, object] = {
    "kind": "actual",
    "category": "food",
    "title": "mine",
    "amount_minor": 1000,
}
EVENT_TRIPS: dict[str, str] = {}  # the trip each event made here is on
ITEM_TRIPS: dict[str, str] = {}  # the trip each checklist item made here is on
EXPENSE_TRIPS: dict[str, str] = {}  # the trip each expense made here is on


async def create_trip(client: AsyncClient, owner: Account) -> str:
    response = await client.post(f"{PREFIX}trips", headers=owner.headers, json=TRIP_BODY)
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def check_trip(client: AsyncClient, owner: Account, trip_id: str) -> None:
    response = await client.get(f"{PREFIX}trips/{trip_id}", headers=owner.headers)
    assert response.status_code == 200
    assert (response.json()["title"], response.json()["revision_no"]) == ("mine", 1)
    events = await client.get(f"{PREFIX}trips/{trip_id}/events", headers=owner.headers)
    assert events.json()["total"] == 1  # only its own making
    for kind in ("activities", "checklist", "bookings", "expenses"):  # nobody's landed on it
        found = await client.get(f"{PREFIX}trips/{trip_id}/{kind}", headers=owner.headers)
        assert found.json() == [], kind


async def create_activity(client: AsyncClient, owner: Account) -> str:
    trip_id = await create_trip(client, owner)
    response = await client.post(
        f"{PREFIX}trips/{trip_id}/activities", headers=owner.headers, json=ACTIVITY_BODY
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def check_activity(client: AsyncClient, owner: Account, activity_id: str) -> None:
    response = await client.get(f"{PREFIX}activities/{activity_id}", headers=owner.headers)
    assert response.status_code == 200
    found = response.json()
    assert (found["title"], found["status"], found["revision_no"]) == ("mine", "planned", 1)


async def create_item(client: AsyncClient, owner: Account) -> str:
    trip_id = await create_trip(client, owner)
    response = await client.post(
        f"{PREFIX}trips/{trip_id}/checklist", headers=owner.headers, json=ITEM_BODY
    )
    assert response.status_code == 201, response.text
    item_id = str(response.json()["id"])
    ITEM_TRIPS[item_id] = trip_id
    return item_id


async def check_item(client: AsyncClient, owner: Account, item_id: str) -> None:
    url = f"{PREFIX}trips/{ITEM_TRIPS[item_id]}/checklist"
    [found] = (await client.get(url, headers=owner.headers)).json()
    assert (found["id"], found["title"], found["state"]) == (item_id, "mine", "not_started")
    assert (found["edited"], found["revision_no"]) == (False, 1)


async def create_booking(client: AsyncClient, owner: Account) -> str:
    trip_id = await create_trip(client, owner)
    response = await client.post(
        f"{PREFIX}trips/{trip_id}/bookings", headers=owner.headers, json=BOOKING_BODY
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def check_booking(client: AsyncClient, owner: Account, booking_id: str) -> None:
    response = await client.get(f"{PREFIX}bookings/{booking_id}", headers=owner.headers)
    assert response.status_code == 200
    found = response.json()
    assert (found["title"], found["status"], found["revision_no"]) == ("mine", "entered", 1)


async def create_expense(client: AsyncClient, owner: Account) -> str:
    trip_id = await create_trip(client, owner)
    response = await client.post(
        f"{PREFIX}trips/{trip_id}/expenses", headers=owner.headers, json=EXPENSE_BODY
    )
    assert response.status_code == 201, response.text
    expense_id = str(response.json()["id"])
    EXPENSE_TRIPS[expense_id] = trip_id
    return expense_id


async def check_expense(client: AsyncClient, owner: Account, expense_id: str) -> None:
    url = f"{PREFIX}trips/{EXPENSE_TRIPS[expense_id]}/expenses"
    [found] = (await client.get(url, headers=owner.headers)).json()
    assert (found["id"], found["title"], found["amount_minor"]) == (expense_id, "mine", 1000)
    assert found["revision_no"] == 1


async def create_event(client: AsyncClient, owner: Account) -> str:
    """An activity's adding: an event that can be undone."""
    activity_id = await create_activity(client, owner)
    url = f"{PREFIX}activities/{activity_id}"
    trip_id = (await client.get(url, headers=owner.headers)).json()["trip_id"]
    page = (await client.get(f"{PREFIX}trips/{trip_id}/events", headers=owner.headers)).json()
    event_id = str(next(item["id"] for item in page["items"] if item["undoable"]))
    EVENT_TRIPS[event_id] = trip_id
    return event_id


async def check_event(client: AsyncClient, owner: Account, event_id: str) -> None:
    url = f"{PREFIX}trips/{EVENT_TRIPS[event_id]}/events"
    page = (await client.get(url, headers=owner.headers)).json()
    assert page["total"] == 2  # its trip's making and this one: no undo by anyone else
    mine = next(item for item in page["items"] if item["id"] == event_id)
    assert (mine["undoable"], mine["undone_at"]) == (True, None)


# Valid bodies for routes that would otherwise fail validation before the lookup.
ROUTE_BODIES: dict[tuple[str, str], dict[str, object]] = {
    ("PATCH", f"{PREFIX}trips/{{trip_id}}"): {"title": "hijacked", "revision_no": 1},
    ("POST", f"{PREFIX}trips/{{trip_id}}/activities"): ACTIVITY_BODY,
    ("POST", f"{PREFIX}trips/{{trip_id}}/activities/reorder"): {
        "day": DAY,
        "activity_ids": [NOBODY],
    },
    ("POST", f"{PREFIX}trips/{{trip_id}}/checklist"): ITEM_BODY,
    ("POST", f"{PREFIX}trips/{{trip_id}}/checklist/suggest"): {"packing": True, "apply": True},
    ("POST", f"{PREFIX}trips/{{trip_id}}/assist"): {"task": "ask", "text": "what is on day one?"},
    ("POST", f"{PREFIX}trips/{{trip_id}}/activities/draft"): {
        "day": DAY,
        "activities": [{"title": "hijacked", "kind": "beach"}],
        "apply": True,
    },
    ("POST", f"{PREFIX}trips/{{trip_id}}/checklist/assist"): {
        "activity_id": NOBODY,
        "items": [{"title": "hijacked"}],
        "apply": True,
    },
    ("POST", f"{PREFIX}trips/{{trip_id}}/bookings"): BOOKING_BODY,
    ("POST", f"{PREFIX}trips/{{trip_id}}/expenses"): EXPENSE_BODY,
    ("PATCH", f"{PREFIX}activities/{{activity_id}}"): {"title": "hijacked", "revision_no": 1},
    ("POST", f"{PREFIX}activities/{{activity_id}}/change"): {"action": "done", "apply": True},
    ("PATCH", f"{PREFIX}checklist/{{item_id}}"): {"state": "completed"},
    ("PATCH", f"{PREFIX}bookings/{{booking_id}}"): {"status": "confirmed", "revision_no": 1},
    ("PATCH", f"{PREFIX}expenses/{{expense_id}}"): {"amount_minor": 1, "revision_no": 1},
}

RESOURCES: dict[
    str,
    tuple[
        Callable[[AsyncClient, Account], Awaitable[str]],
        Callable[[AsyncClient, Account, str], Awaitable[None]],
    ],
] = {
    "trip_id": (create_trip, check_trip),
    "activity_id": (create_activity, check_activity),
    "item_id": (create_item, check_item),
    "booking_id": (create_booking, check_booking),
    "expense_id": (create_expense, check_expense),
    "event_id": (create_event, check_event),
}
