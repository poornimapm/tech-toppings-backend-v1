"""Activities: added last in their day and kept inside the trip's dates, edited as of the revision
seen, reordered only by ids checked against that day of that trip, and undone from the history."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from tests.helpers import Account, register
from tests.modules.travel.conftest import (
    ACTIVITIES,
    FIRST_DAY,
    LAST_DAY,
    SECOND_DAY,
    TRIPS,
    add_activity,
    add_trip,
    history,
    newest_event,
    read_activity,
    stored,
    timeline,
    undo,
)


async def reorder(
    client: AsyncClient, account: Account, trip_id: str, ids: list[str], day: str = FIRST_DAY
) -> Any:
    return await client.post(
        f"{TRIPS}/{trip_id}/activities/reorder",
        headers=account.headers,
        json={"day": day, "activity_ids": ids},
    )


async def test_an_activity_is_added_read_edited_and_removed(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)

    activity = await add_activity(
        client,
        account,
        trip["id"],
        title="  Phi Phi tour  ",
        kind="tour",
        start_time="08:30",
        duration_minutes=240,
        location="Rassada pier",
        transport="Speedboat",
        estimated_cost_minor=350000,
    )
    assert activity["title"] == "Phi Phi tour"
    assert (activity["status"], activity["confirmed"], activity["position"]) == (
        "planned",
        False,
        0,
    )
    url = f"{ACTIVITIES}/{activity['id']}"
    assert await read_activity(client, account, activity["id"]) == activity

    edited = await client.patch(
        url,
        headers=account.headers,
        json={"confirmed": True, "notes": "Bring cash", "revision_no": 1},
    )
    assert edited.status_code == 200, edited.text
    assert (edited.json()["confirmed"], edited.json()["revision_no"]) == (True, 2)
    assert edited.json()["location"] == "Rassada pier"  # only what was sent

    removal = await client.delete(url, headers=account.headers)
    assert removal.status_code == 200, removal.text
    assert removal.json() == {"event_id": await newest_event(client, account, trip["id"])}
    assert (await client.get(url, headers=account.headers)).status_code == 404
    assert await timeline(client, account, trip["id"], FIRST_DAY) == []
    assert [event["kind"] for event in await history(client, account, trip["id"])] == [
        "activity_deleted",
        "activity_edited",
        "activity_added",
        "trip_created",
    ]


async def test_an_activity_is_kept_once_per_idempotency_key(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    headers = {**account.headers, "Idempotency-Key": "activity-key-1"}
    body = {"title": "Beach", "kind": "beach", "day": FIRST_DAY}
    url = f"{TRIPS}/{trip['id']}/activities"

    first = await client.post(url, headers=headers, json=body)
    second = await client.post(url, headers=headers, json=body)

    assert second.json()["id"] == first.json()["id"]
    assert second.headers["Idempotent-Replayed"] == "true"
    assert len(await history(client, account, trip["id"])) == 2  # its trip's, and one for it


async def test_each_day_keeps_its_own_order(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    for title, day in (("a", FIRST_DAY), ("x", SECOND_DAY), ("b", FIRST_DAY)):
        await add_activity(client, account, trip["id"], title=title, day=day)

    everything = await client.get(f"{TRIPS}/{trip['id']}/activities", headers=account.headers)

    assert [item["title"] for item in everything.json()] == ["a", "b", "x"]
    assert await timeline(client, account, trip["id"], FIRST_DAY) == ["a", "b"]
    assert await timeline(client, account, trip["id"], SECOND_DAY) == ["x"]


@pytest.mark.parametrize("day", ["2030-02-28", "2030-03-06"])
async def test_an_activity_stays_within_its_trips_dates(client: AsyncClient, day: str) -> None:
    account = await register(client)
    trip = await add_trip(client, account)

    response = await client.post(
        f"{TRIPS}/{trip['id']}/activities",
        headers=account.headers,
        json={"title": "Beach", "kind": "beach", "day": day},
    )

    assert (response.status_code, response.json()["code"]) == (422, "TRAVEL_DAY_OUTSIDE_TRIP")


@pytest.mark.parametrize(
    "fields",
    [{"kind": "teleport"}, {"start_time": "25:00"}, {"duration_minutes": 0}, {"title": " "}],
)
async def test_what_an_activity_can_not_be(client: AsyncClient, fields: dict[str, Any]) -> None:
    account = await register(client)
    trip = await add_trip(client, account)

    response = await client.post(
        f"{TRIPS}/{trip['id']}/activities",
        headers=account.headers,
        json={"title": "Beach", "kind": "beach", "day": FIRST_DAY, **fields},
    )

    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_ERROR")


async def test_an_edit_can_not_move_an_activity_to_another_day(client: AsyncClient) -> None:
    """A reschedule does, with what depends on it."""
    account = await register(client)
    trip = await add_trip(client, account)
    activity = await add_activity(client, account, trip["id"])

    response = await client.patch(
        f"{ACTIVITIES}/{activity['id']}",
        headers=account.headers,
        json={"day": SECOND_DAY, "revision_no": 1},
    )

    assert response.status_code == 422


async def test_a_booking_linked_must_be_on_the_trip(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    activity = await add_activity(client, account, trip["id"])
    someone_elses = "0" * 24

    added = await client.post(
        f"{TRIPS}/{trip['id']}/activities",
        headers=account.headers,
        json={"title": "Tour", "kind": "tour", "day": FIRST_DAY, "booking_id": someone_elses},
    )
    edited = await client.patch(
        f"{ACTIVITIES}/{activity['id']}",
        headers=account.headers,
        json={"booking_id": someone_elses, "revision_no": 1},
    )

    for response in (added, edited):
        assert (response.status_code, response.json()["code"]) == (422, "TRAVEL_BOOKING_UNKNOWN")


async def test_a_stale_edit_of_an_activity_is_refused(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    activity = await add_activity(client, account, trip["id"])
    url = f"{ACTIVITIES}/{activity['id']}"
    first = await client.patch(url, headers=account.headers, json={"title": "A", "revision_no": 1})
    assert first.status_code == 200, first.text

    stale = await client.patch(url, headers=account.headers, json={"title": "B", "revision_no": 1})

    assert (stale.status_code, stale.json()["code"]) == (409, "VERSION_CONFLICT")
    assert (await read_activity(client, account, activity["id"]))["title"] == "A"


async def test_a_deleted_trips_activities_are_out_of_reach(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    activity = await add_activity(client, account, trip["id"])
    url = f"{ACTIVITIES}/{activity['id']}"
    await client.delete(f"{TRIPS}/{trip['id']}", headers=account.headers)

    assert (await client.get(url, headers=account.headers)).status_code == 404

    await client.post(f"{TRIPS}/{trip['id']}/undelete", headers=account.headers)
    assert (await client.get(url, headers=account.headers)).status_code == 200


async def test_a_day_is_reordered_and_only_what_moved_is_written(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    a, b, c = [
        await add_activity(client, account, trip["id"], title=title) for title in ("a", "b", "c")
    ]
    other = await add_activity(client, account, trip["id"], title="x", day=SECOND_DAY)

    response = await reorder(client, account, trip["id"], [b["id"], a["id"], c["id"]])

    assert response.status_code == 200, response.text
    assert [item["title"] for item in response.json()] == ["b", "a", "c"]
    assert await timeline(client, account, trip["id"], FIRST_DAY) == ["b", "a", "c"]
    # c kept its place, and the other day wasn't touched.
    assert (await read_activity(client, account, c["id"]))["revision_no"] == 1
    assert await read_activity(client, account, other["id"]) == other
    event = (await history(client, account, trip["id"]))[0]
    assert event["kind"] == "activities_reordered"
    assert {change["record_id"] for change in event["changes"]} == {a["id"], b["id"]}
    # Each of the day, c too, says which event the reorder recorded.
    assert {item["event_id"] for item in response.json()} == {event["id"]}


async def test_a_reorder_that_moves_nothing_writes_nothing(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    a = await add_activity(client, account, trip["id"], title="a")
    b = await add_activity(client, account, trip["id"], title="b")
    before = await stored(account)

    response = await reorder(client, account, trip["id"], [a["id"], b["id"]])

    assert response.status_code == 200, response.text
    assert await stored(account) == before
    assert [item["event_id"] for item in response.json()] == [None, None]


async def test_a_reorder_is_undone_from_the_history(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    a = await add_activity(client, account, trip["id"], title="a")
    b = await add_activity(client, account, trip["id"], title="b")
    await reorder(client, account, trip["id"], [b["id"], a["id"]])
    event = (await history(client, account, trip["id"]))[0]

    assert (await undo(client, account, event["id"])).status_code == 200

    assert await timeline(client, account, trip["id"], FIRST_DAY) == ["a", "b"]


async def test_a_reorders_ids_are_checked_against_that_day_of_that_trip(
    client: AsyncClient, other_client: AsyncClient
) -> None:
    account, stranger = await register(client), await register(other_client)
    trip, elsewhere = await add_trip(client, account), await add_trip(client, account)
    a = await add_activity(client, account, trip["id"], title="a")
    b = await add_activity(client, account, trip["id"], title="b")
    next_day = await add_activity(client, account, trip["id"], title="n", day=SECOND_DAY)
    other_trip = await add_activity(client, account, elsewhere["id"], title="o")
    their_trip = await add_trip(other_client, stranger)
    theirs = await add_activity(other_client, stranger, their_trip["id"], title="t")
    before = await stored(account)

    for ids in (
        [a["id"]],  # one left out
        [a["id"], a["id"]],  # one twice
        [a["id"], b["id"], next_day["id"]],  # another day's
        [a["id"], b["id"], other_trip["id"]],  # another trip's
        [a["id"], b["id"], theirs["id"]],  # another person's
        [a["id"], "000000000000000000000000"],  # nobody's
    ):
        response = await reorder(client, account, trip["id"], ids)
        assert (response.status_code, response.json()["code"]) == (422, "TRAVEL_REORDER_MISMATCH")

    assert await stored(account) == before
    assert await read_activity(other_client, stranger, theirs["id"]) == theirs


async def test_reordering_another_persons_trip_answers_not_found(
    client: AsyncClient, other_client: AsyncClient
) -> None:
    owner, intruder = await register(client), await register(other_client)
    trip = await add_trip(client, owner)
    a = await add_activity(client, owner, trip["id"])

    response = await reorder(other_client, intruder, trip["id"], [a["id"]])

    assert (response.status_code, response.json()["code"]) == (404, "NOT_FOUND")


async def test_an_added_or_removed_activity_is_undone_from_the_history(
    client: AsyncClient,
) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    kept = await add_activity(client, account, trip["id"], title="kept", day=LAST_DAY)
    gone = await add_activity(client, account, trip["id"], title="gone", day=LAST_DAY)
    added = (await history(client, account, trip["id"]))[0]

    assert (await undo(client, account, added["id"])).status_code == 200
    assert await timeline(client, account, trip["id"], LAST_DAY) == ["kept"]

    await client.delete(f"{ACTIVITIES}/{kept['id']}", headers=account.headers)
    removal = (await history(client, account, trip["id"]))[0]
    assert removal["kind"] == "activity_deleted"
    assert (await undo(client, account, removal["id"])).status_code == 200
    assert await timeline(client, account, trip["id"], LAST_DAY) == ["kept"]
    assert (
        await client.get(f"{ACTIVITIES}/{gone['id']}", headers=account.headers)
    ).status_code == 404
