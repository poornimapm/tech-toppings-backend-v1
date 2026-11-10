"""Bookings: kept as the person entered them and undone from the history, linked only from the same
trip's activities (rule 6), kept while linked, on the day's plan, and listed for review, never
changed, when what they are for changes (rule 3)."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from tests.helpers import Account, register
from tests.modules.travel.conftest import (
    ACTIVITIES,
    BOOKINGS,
    FIRST_DAY,
    SECOND_DAY,
    TRIPS,
    add_activity,
    add_booking,
    add_expense,
    add_trip,
    change,
    history,
    stored,
    undo,
)


async def link(
    client: AsyncClient, account: Account, activity: dict[str, Any], booking_id: Any
) -> Any:
    response = await client.patch(
        f"{ACTIVITIES}/{activity['id']}",
        headers=account.headers,
        json={"booking_id": booking_id, "revision_no": activity["revision_no"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_a_booking_is_added_read_edited_and_removed(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)

    made = await add_booking(
        client,
        account,
        trip["id"],
        kind="flight",
        title="  Chennai to Goa  ",
        reference="PNR123",
        starts_at=f"{FIRST_DAY}T06:30",
        ends_at=f"{FIRST_DAY}T08:45",
        link="https://example.com/my-booking",
    )
    assert made["title"] == "Chennai to Goa"
    assert (made["status"], made["revision_no"], made["location"]) == ("entered", 1, None)
    url = f"{BOOKINGS}/{made['id']}"
    assert (await client.get(url, headers=account.headers)).json() == made

    edited = await client.patch(
        url, headers=account.headers, json={"status": "confirmed", "revision_no": 1}
    )
    assert edited.status_code == 200, edited.text
    assert (edited.json()["status"], edited.json()["reference"]) == ("confirmed", "PNR123")

    removed = await client.delete(url, headers=account.headers)
    assert removed.status_code == 200, removed.text
    assert (await client.get(url, headers=account.headers)).status_code == 404
    listed = await client.get(f"{TRIPS}/{trip['id']}/bookings", headers=account.headers)
    assert listed.json() == []
    removal, editing, adding, _ = await history(client, account, trip["id"])
    assert [removal["kind"], editing["kind"], adding["kind"]] == [
        "booking_removed",
        "booking_edited",
        "booking_added",
    ]
    assert (removed.json(), edited.json()["event_id"]) == (
        {"event_id": removal["id"]},
        editing["id"],
    )

    assert (await undo(client, account, removal["id"])).status_code == 200
    back = (await client.get(url, headers=account.headers)).json()
    assert (back["title"], back["status"]) == ("Chennai to Goa", "confirmed")


async def test_bookings_are_listed_those_with_no_time_first(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    later = await add_booking(client, account, trip["id"], starts_at=f"{SECOND_DAY}T09:00")
    sooner = await add_booking(client, account, trip["id"], starts_at=f"{FIRST_DAY}T09:00")
    visa = await add_booking(client, account, trip["id"], kind="visa", title="Visa")

    listed = await client.get(f"{TRIPS}/{trip['id']}/bookings", headers=account.headers)

    assert [item["id"] for item in listed.json()] == [visa["id"], sooner["id"], later["id"]]


async def test_a_booking_is_kept_once_per_idempotency_key(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    headers = {**account.headers, "Idempotency-Key": "booking-key-0001"}
    url = f"{TRIPS}/{trip['id']}/bookings"

    first = await client.post(url, headers=headers, json={"kind": "hotel", "title": "Stay"})
    second = await client.post(url, headers=headers, json={"kind": "hotel", "title": "Again"})

    assert (first.status_code, second.status_code) == (201, 201)
    assert (second.json()["id"], second.json()["title"]) == (first.json()["id"], "Stay")
    assert second.headers["Idempotent-Replayed"] == "true"
    assert [item["kind"] for item in await history(client, account, trip["id"])] == [
        "booking_added",
        "trip_created",
    ]


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "extracted"},  # only the app reads a document
        {"status": "verified"},  # only the app checks with a provider
        {"kind": "cruise"},
        {"starts_at": "2030-02-30T10:00"},
        {"starts_at": "2030-03-01 10:00"},
        {"starts_at": f"{SECOND_DAY}T10:00", "ends_at": f"{FIRST_DAY}T10:00"},
        {"link": "javascript:alert(1)"},
        {"link": "example.com"},
    ],
)
async def test_what_a_booking_can_not_be(client: AsyncClient, fields: dict[str, Any]) -> None:
    account = await register(client)
    trip = await add_trip(client, account)

    response = await client.post(
        f"{TRIPS}/{trip['id']}/bookings",
        headers=account.headers,
        json={"kind": "tour", "title": "Tour", **fields},
    )

    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_ERROR")
    assert (await stored(account))["travel_bookings"] == []


async def test_an_edit_that_leaves_a_booking_ending_before_it_starts_is_refused(
    client: AsyncClient,
) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    booking = await add_booking(client, account, trip["id"], starts_at=f"{SECOND_DAY}T10:00")

    response = await client.patch(
        f"{BOOKINGS}/{booking['id']}",
        headers=account.headers,
        json={"ends_at": f"{FIRST_DAY}T10:00", "revision_no": 1},
    )

    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_ERROR")


async def test_a_stale_edit_of_a_booking_is_refused(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    booking = await add_booking(client, account, trip["id"])
    url = f"{BOOKINGS}/{booking['id']}"
    await client.patch(url, headers=account.headers, json={"title": "A", "revision_no": 1})

    stale = await client.patch(url, headers=account.headers, json={"title": "B", "revision_no": 1})

    assert (stale.status_code, stale.json()["code"]) == (409, "VERSION_CONFLICT")
    assert (await client.get(url, headers=account.headers)).json()["title"] == "A"


async def test_an_activity_links_only_to_a_booking_of_its_own_trip(client: AsyncClient) -> None:
    account = await register(client)
    trip, other_trip = await add_trip(client, account), await add_trip(client, account)
    mine = await add_booking(client, account, trip["id"])
    elsewhere = await add_booking(client, account, other_trip["id"])
    gone = await add_booking(client, account, trip["id"], title="Gone")
    await client.delete(f"{BOOKINGS}/{gone['id']}", headers=account.headers)

    linked = await add_activity(client, account, trip["id"], kind="tour", booking_id=mine["id"])
    assert linked["booking_id"] == mine["id"]
    activity = await add_activity(client, account, trip["id"])
    for booking_id in (elsewhere["id"], gone["id"]):
        added = await client.post(
            f"{TRIPS}/{trip['id']}/activities",
            headers=account.headers,
            json={"title": "Tour", "kind": "tour", "day": FIRST_DAY, "booking_id": booking_id},
        )
        edited = await client.patch(
            f"{ACTIVITIES}/{activity['id']}",
            headers=account.headers,
            json={"booking_id": booking_id, "revision_no": 1},
        )
        for response in (added, edited):
            assert (response.status_code, response.json()["code"]) == (
                422,
                "TRAVEL_BOOKING_UNKNOWN",
            )


async def test_a_linked_booking_stays_until_nothing_links_to_it(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    booking = await add_booking(client, account, trip["id"])
    activity = await add_activity(client, account, trip["id"], booking_id=booking["id"])
    url = f"{BOOKINGS}/{booking['id']}"

    refused = await client.delete(url, headers=account.headers)

    assert (refused.status_code, refused.json()["code"]) == (409, "TRAVEL_BOOKING_IN_USE")
    assert refused.json()["details"] == {"activity_ids": [activity["id"]]}
    await link(client, account, activity, None)
    assert (await client.delete(url, headers=account.headers)).status_code == 200


async def test_a_bookings_adding_is_not_undone_while_an_activity_links_to_it(
    client: AsyncClient,
) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    booking = await add_booking(client, account, trip["id"])
    adding = (await history(client, account, trip["id"]))[0]
    await add_activity(client, account, trip["id"], booking_id=booking["id"])

    refused = await undo(client, account, adding["id"])

    assert (refused.status_code, refused.json()["code"]) == (409, "TRAVEL_UNDO_STALE")
    assert (await client.get(f"{BOOKINGS}/{booking['id']}", headers=account.headers)).is_success


async def test_an_undo_never_links_an_activity_to_a_booking_removed_since(
    client: AsyncClient,
) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    booking = await add_booking(client, account, trip["id"])
    activity = await add_activity(client, account, trip["id"], booking_id=booking["id"])
    await link(client, account, activity, None)
    unlinking = (await history(client, account, trip["id"]))[0]
    assert (await client.delete(f"{BOOKINGS}/{booking['id']}", headers=account.headers)).is_success

    refused = await undo(client, account, unlinking["id"])

    assert (refused.status_code, refused.json()["code"]) == (409, "TRAVEL_UNDO_STALE")
    found = await client.get(f"{ACTIVITIES}/{activity['id']}", headers=account.headers)
    assert found.json()["booking_id"] is None


async def test_a_days_plan_has_the_bookings_it_needs(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    trip_id = trip["id"]
    flight = await add_booking(
        client, account, trip_id, kind="flight", starts_at=f"{SECOND_DAY}T06:30"
    )
    checkout = await add_booking(
        client,
        account,
        trip_id,
        kind="hotel",
        starts_at=f"{FIRST_DAY}T14:00",
        ends_at=f"{SECOND_DAY}T11:00",
    )
    tour = await add_booking(client, account, trip_id)  # no time: there through its activity
    skipped = await add_booking(client, account, trip_id, title="Skipped")
    await add_booking(client, account, trip_id, title="Another day", starts_at=f"{FIRST_DAY}T09:00")
    await add_activity(client, account, trip_id, day=SECOND_DAY, booking_id=tour["id"])
    off = await add_activity(client, account, trip_id, day=SECOND_DAY, booking_id=skipped["id"])
    assert (await change(client, account, off["id"], action="skip", apply=True)).is_success

    plan = await client.get(f"{TRIPS}/{trip_id}/prepare?when={SECOND_DAY}", headers=account.headers)

    assert plan.status_code == 200, plan.text
    assert [item["id"] for item in plan.json()["bookings"]] == [
        tour["id"],
        checkout["id"],
        flight["id"],
    ]


async def test_a_skip_lists_its_booking_and_estimates_for_review_and_changes_neither(
    client: AsyncClient,
) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    booking = await add_booking(client, account, trip["id"])
    activity = await add_activity(
        client,
        account,
        trip["id"],
        kind="tour",
        booking_id=booking["id"],
        estimated_cost_minor=5000,
    )
    estimate = await add_expense(
        client, account, trip["id"], kind="estimate", title="Tour fee", activity_id=activity["id"]
    )
    await add_expense(client, account, trip["id"], title="Deposit", activity_id=activity["id"])
    await add_expense(client, account, trip["id"], kind="estimate", title="Not tied")
    before = await stored(account)

    answer = (await change(client, account, activity["id"], action="skip", apply=True)).json()

    assert answer["needs_review"] == [
        {
            "entity": "activity",
            "record_id": activity["id"],
            "title": "Beach",
            "reason": "estimate",
            "suggestion_key": None,
        },
        {
            "entity": "booking",
            "record_id": booking["id"],
            "title": "Island tour",
            "reason": "linked",
            "suggestion_key": None,
        },
        {
            "entity": "expense",
            "record_id": estimate["id"],
            "title": "Tour fee",
            "reason": "estimate",
            "suggestion_key": None,
        },
    ]
    after = await stored(account)
    for name in (
        "travel_bookings",
        "travel_booking_revisions",
        "travel_expenses",
        "travel_expense_revisions",
    ):
        assert after[name] == before[name], name
    assert {item["entity"] for item in answer["what_changed"]} == {"activity"}


@pytest.mark.parametrize(
    ("body", "listed"),
    [
        ({"action": "reschedule", "new_day": SECOND_DAY}, ["booking"]),  # not then: check it
        ({"action": "done"}, []),  # it happened as booked
    ],
    ids=["reschedule", "done"],
)
async def test_what_else_lists_a_linked_booking(
    client: AsyncClient, body: dict[str, Any], listed: list[str]
) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    booking = await add_booking(client, account, trip["id"])
    activity = await add_activity(client, account, trip["id"], booking_id=booking["id"])
    await add_expense(client, account, trip["id"], kind="estimate", activity_id=activity["id"])

    answer = (await change(client, account, activity["id"], **body)).json()

    assert [item["entity"] for item in answer["needs_review"]] == listed
