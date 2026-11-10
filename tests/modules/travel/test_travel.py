"""Trips: their routes round trip, each traveller stays their own, the Premium lock and the trip's
length hold, a stale edit is refused, a mode switch changes only the mode, and a person's export
and erasure cover everything Travel keeps."""

from __future__ import annotations

import datetime as dt
import inspect
import json
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from beanie import Document
from httpx import AsyncClient

from app.modules.travel import models, trips
from app.modules.travel.manifest import MANIFEST
from app.modules.travel.rules import rules
from tests.helpers import FrozenClock, register
from tests.modules.travel.conftest import (
    FIRST_DAY,
    LAST_DAY,
    SECOND_DAY,
    TRAVEL,
    TRIPS,
    add_activity,
    add_booking,
    add_expense,
    add_trip,
    history,
    made,
    stored,
    trip_body,
)

WEST, EAST = "Pacific/Pago_Pago", "Pacific/Kiritimati"  # 25 hours apart: never the same day


async def test_a_trip_is_made_read_changed_deleted_and_brought_back(client: AsyncClient) -> None:
    account = await register(client)
    headers = account.headers

    answer = await client.post(
        TRIPS,
        headers=headers,
        json=trip_body(
            title="  Honeymoon  ",
            destinations=[{"name": "Phuket", "country": "Thailand", "timezone": "Asia/Bangkok"}],
            departure_city="Chennai",
            trip_type="honeymoon",
            pace="relaxed",
            budget_minor=25000000,
            interests=["beaches"],
            notes="Our first long trip",
            remind_prepare=True,
        ),
    )
    trip = made(answer)
    assert trip["title"] == "Honeymoon"  # spaces around it are dropped
    assert (trip["mode"], trip["currency"], trip["revision_no"]) == ("static", "INR", 1)
    assert trip["destinations"][0]["timezone"] == "Asia/Bangkok"
    assert trip["remind_prepare"] is True
    url = f"{TRIPS}/{trip['id']}"
    assert (await client.get(url, headers=headers)).json() == trip

    changed = await client.patch(url, headers=headers, json={"notes": "Changed", "revision_no": 1})
    assert changed.status_code == 200, changed.text
    assert (changed.json()["notes"], changed.json()["revision_no"]) == ("Changed", 2)
    assert changed.json()["pace"] == "relaxed"  # only what was sent

    assert (await client.delete(url, headers=headers)).status_code == 204
    assert (await client.get(url, headers=headers)).status_code == 404
    assert (await client.get(TRIPS, headers=headers)).json()["total"] == 0

    back = await client.post(f"{url}/undelete", headers=headers)
    assert (back.status_code, back.json()["notes"]) == (200, "Changed")
    assert (await client.get(TRIPS, headers=headers)).json()["total"] == 1
    events = await history(client, account, trip["id"])
    assert [event["kind"] for event in events] == [
        "trip_restored",
        "trip_deleted",
        "trip_edited",
        "trip_created",
    ]
    restored, _, edited, created = (event["id"] for event in events)
    assert (answer.json()["event_id"], changed.json()["event_id"]) == (created, edited)
    assert back.json()["event_id"] == restored
    again = await client.post(f"{url}/undelete", headers=headers)  # there already: no event
    assert (again.status_code, again.json()["event_id"]) == (200, None)


async def test_a_trip_is_kept_once_per_idempotency_key(client: AsyncClient) -> None:
    account = await register(client)
    headers = {**account.headers, "Idempotency-Key": "trip-key-0001"}

    first = await client.post(TRIPS, headers=headers, json=trip_body())
    second = await client.post(TRIPS, headers=headers, json=trip_body(title="Again"))

    assert (first.status_code, second.status_code) == (201, 201)
    assert second.json()["id"] == first.json()["id"]
    assert second.headers["Idempotent-Replayed"] == "true"
    assert second.json()["title"] == "Goa"
    assert [item["kind"] for item in await history(client, account, first.json()["id"])] == [
        "trip_created"
    ]


async def test_each_traveller_is_their_own_and_keeps_their_id(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(
        client,
        account,
        travellers=[
            {"name": "Asha", "passport_country": "India", "passport_expiry": "2032-01-31"},
            {"name": "Ravi", "passport_country": "Singapore", "dietary": "Vegetarian"},
        ],
    )
    asha, ravi = trip["travellers"]
    assert (asha["passport_country"], ravi["passport_country"]) == ("India", "Singapore")
    assert ravi["residence_country"] is None  # nothing is inferred
    assert asha["id"] != ravi["id"]

    url = f"{TRIPS}/{trip['id']}"
    joined = await client.patch(
        url,
        headers=account.headers,
        json={"travellers": [asha, {"name": "Meena"}], "revision_no": 1},
    )
    assert joined.status_code == 200, joined.text
    now = joined.json()["travellers"]
    assert now[0] == asha
    assert (now[1]["name"], now[1]["passport_country"]) == ("Meena", None)
    assert now[1]["id"]


@pytest.mark.parametrize("sent", [["ghost"], ["known", "known"]])
async def test_a_travellers_id_is_checked_against_the_trip(
    client: AsyncClient, sent: list[str]
) -> None:
    account = await register(client)
    trip = await add_trip(client, account, travellers=[{"name": "Asha"}])
    known = trip["travellers"][0]["id"]
    people = [{"id": known if name == "known" else name, "name": "Someone"} for name in sent]

    response = await client.patch(
        f"{TRIPS}/{trip['id']}",
        headers=account.headers,
        json={"travellers": people, "revision_no": 1},
    )

    assert (response.status_code, response.json()["code"]) == (422, "TRAVEL_TRAVELLER_UNKNOWN")


async def test_a_new_trip_can_not_name_a_traveller_id(client: AsyncClient) -> None:
    headers = (await register(client)).headers

    response = await client.post(
        TRIPS, headers=headers, json=trip_body(travellers=[{"id": "ghost", "name": "Asha"}])
    )

    assert (response.status_code, response.json()["code"]) == (422, "TRAVEL_TRAVELLER_UNKNOWN")


async def test_premium_is_locked_on_a_new_trip_and_an_existing_one(client: AsyncClient) -> None:
    account = await register(client)

    made = await client.post(TRIPS, headers=account.headers, json=trip_body(mode="premium"))
    assert (made.status_code, made.json()["code"]) == (403, "TRAVEL_PREMIUM_LOCKED")
    assert (await stored(account))["travel_trips"] == []

    trip = await add_trip(client, account, mode="basic")  # Basic can be saved
    assert trip["mode"] == "basic"
    before = await stored(account)
    switched = await client.patch(
        f"{TRIPS}/{trip['id']}", headers=account.headers, json={"mode": "premium", "revision_no": 1}
    )
    assert (switched.status_code, switched.json()["code"]) == (403, "TRAVEL_PREMIUM_LOCKED")
    assert await stored(account) == before

    options = await client.get(f"{TRAVEL}/options", headers=account.headers)
    assert options.json()["premium_available"] is False


async def test_one_small_function_is_what_locks_premium(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = await register(client)
    monkeypatch.setattr(trips, "premium_allowed", lambda _ctx: True)

    trip = await add_trip(client, account, mode="premium")

    assert trip["mode"] == "premium"


async def test_a_trip_can_be_as_long_as_the_rule_and_no_longer(client: AsyncClient) -> None:
    account = await register(client)
    start = dt.date.fromisoformat(FIRST_DAY)
    longest = rules().trips.max_days
    last = (start + dt.timedelta(days=longest - 1)).isoformat()
    beyond = (start + dt.timedelta(days=longest)).isoformat()

    trip = await add_trip(client, account, end_date=last)
    too_long = await client.post(TRIPS, headers=account.headers, json=trip_body(end_date=beyond))
    stretched = await client.patch(
        f"{TRIPS}/{trip['id']}",
        headers=account.headers,
        json={"end_date": beyond, "revision_no": 1},
    )

    for response in (too_long, stretched):
        assert (response.status_code, response.json()["code"]) == (422, "TRAVEL_TRIP_DATES_INVALID")
        assert response.json()["details"] == {"reason": "too_long", "max_days": longest}


async def test_a_trip_ends_on_or_after_its_first_day(client: AsyncClient) -> None:
    account = await register(client)
    same_day = await add_trip(client, account, end_date=FIRST_DAY)

    response = await client.post(
        TRIPS,
        headers=account.headers,
        json=trip_body(start_date=SECOND_DAY, end_date=FIRST_DAY),
    )

    assert same_day["end_date"] == FIRST_DAY
    assert (response.status_code, response.json()["code"]) == (422, "TRAVEL_TRIP_DATES_INVALID")
    assert response.json()["details"] == {"reason": "end_before_start"}


async def test_a_trips_dates_always_hold_its_activities(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    await add_activity(client, account, trip["id"], day=LAST_DAY)
    url = f"{TRIPS}/{trip['id']}"

    shortened = await client.patch(
        url, headers=account.headers, json={"end_date": SECOND_DAY, "revision_no": 1}
    )

    assert (shortened.status_code, shortened.json()["code"]) == (
        409,
        "TRAVEL_ACTIVITIES_OUTSIDE_DATES",
    )
    assert shortened.json()["details"] == {"days": [LAST_DAY]}
    assert (await client.get(url, headers=account.headers)).json()["end_date"] == LAST_DAY


@pytest.mark.parametrize(
    "fields",
    [
        {"destinations": []},
        {"destinations": [{"name": "Goa", "country": "India", "timezone": "Asia"}]},
        {"destinations": [{"name": "Goa", "country": "India", "timezone": "Mars/Base"}]},
        {"trip_type": "cruise"},
        {"mode": "turbo"},
        {"currency": "rupees"},
        {"budget_minor": -1},
        {"start_date": "01-03-2030"},
    ],
)
async def test_what_a_trip_can_not_be(client: AsyncClient, fields: dict[str, Any]) -> None:
    headers = (await register(client)).headers

    response = await client.post(TRIPS, headers=headers, json=trip_body(**fields))

    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_ERROR")


async def test_a_stale_edit_of_a_trip_is_refused(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    url = f"{TRIPS}/{trip['id']}"
    first = await client.patch(url, headers=account.headers, json={"title": "A", "revision_no": 1})
    assert first.status_code == 200, first.text

    stale = await client.patch(url, headers=account.headers, json={"title": "B", "revision_no": 1})

    assert (stale.status_code, stale.json()["code"]) == (409, "VERSION_CONFLICT")
    assert stale.json()["details"] == {"current": 2}
    assert (await client.get(url, headers=account.headers)).json()["title"] == "A"


async def test_a_mode_switch_changes_only_the_mode(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(
        client,
        account,
        travellers=[{"name": "Asha", "passport_country": "India"}],
        interests=["food"],
        notes="Keep me",
    )
    await add_activity(client, account, trip["id"], estimated_cost_minor=5000, notes="Mine")
    before = await stored(account)

    switched = await client.patch(
        f"{TRIPS}/{trip['id']}", headers=account.headers, json={"mode": "basic", "revision_no": 1}
    )

    assert (switched.status_code, switched.json()["mode"]) == (200, "basic")
    after = await stored(account)
    for name in ("travel_activities", "travel_activity_revisions", "travel_events"):
        assert after[name] == before[name], name
    old, new = before["travel_trips"][0], after["travel_trips"][0]
    assert {name for name in new if new[name] != old[name]} <= {
        "mode",
        "revision_no",
        "updated_at",
    }
    assert new["mode"] == "basic"
    # The trip's own history keeps it: one revision, of that one field.
    added = after["travel_trip_revisions"][len(before["travel_trip_revisions"]) :]
    assert [[item["field"] for item in row["changes"]] for row in added] == [["mode"]]


async def test_a_trip_is_undone_like_anything_else(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    url = f"{TRIPS}/{trip['id']}"
    edited = await client.patch(
        url, headers=account.headers, json={"title": "Kerala", "mode": "basic", "revision_no": 1}
    )
    assert edited.status_code == 200, edited.text
    event = (await history(client, account, trip["id"]))[0]
    assert event["kind"] == "trip_edited"
    assert {item["field"] for item in event["changes"][0]["fields"]} == {"title", "mode"}

    undone = await client.post(f"{TRAVEL}/events/{event['id']}/undo", headers=account.headers)

    assert undone.status_code == 200, undone.text
    back = (await client.get(url, headers=account.headers)).json()
    assert (back["title"], back["mode"]) == ("Goa", "static")


@pytest.mark.parametrize(
    ("phase", "zone", "expected"),
    [("active", EAST, True), ("upcoming", EAST, False), ("upcoming", WEST, True)],
)
async def test_a_trips_today_is_at_its_first_destination(
    client: AsyncClient, clock: FrozenClock, phase: str, zone: str, expected: bool
) -> None:
    """Today in the far east is always tomorrow or later in the far west."""
    account = await register(client)
    day = clock.now().astimezone(ZoneInfo(EAST)).date().isoformat()
    place = {"name": "Far", "country": "Away", "timezone": zone}
    trip = await add_trip(client, account, destinations=[place], start_date=day, end_date=day)

    page = await client.get(f"{TRIPS}?phase={phase}", headers=account.headers)

    assert ([item["id"] for item in page.json()["items"]] == [trip["id"]]) is expected
    assert trip["phase"] == ("active" if zone == EAST else "upcoming")


async def test_trips_are_listed_by_where_they_stand(
    client: AsyncClient, clock: FrozenClock
) -> None:
    account = await register(client)
    today = clock.now().date()

    def days(first: int, last: int) -> dict[str, str]:
        return {
            "start_date": (today + dt.timedelta(days=first)).isoformat(),
            "end_date": (today + dt.timedelta(days=last)).isoformat(),
        }

    await add_trip(client, account, title="Later", **days(40, 42))
    await add_trip(client, account, title="Soon", **days(20, 22))
    await add_trip(client, account, title="Now", **days(-3, 3))
    await add_trip(client, account, title="Long ago", **days(-40, -38))
    await add_trip(client, account, title="Lately", **days(-20, -18))

    async def titles(phase: str) -> list[str]:
        page = await client.get(f"{TRIPS}?phase={phase}", headers=account.headers)
        return [item["title"] for item in page.json()["items"]]

    assert await titles("upcoming") == ["Soon", "Later"]
    assert await titles("active") == ["Now"]
    assert await titles("past") == ["Lately", "Long ago"]
    wrong = await client.get(f"{TRIPS}?phase=someday", headers=account.headers)
    assert wrong.status_code == 422


async def test_the_tile_counts_the_persons_own_trips(
    client: AsyncClient, other_client: AsyncClient
) -> None:
    owner, other = await register(client), await register(other_client)
    await add_trip(client, owner)
    await add_trip(client, owner)
    await add_trip(other_client, other)

    view = (await client.get("/v1/modules/travel", headers=owner.headers)).json()

    assert (view["status"], view["available"]) == ("beta", True)
    assert view["stat"] == {"value": 2, "format": "number", "currency": None, "label": "items"}


async def test_the_options_say_what_the_forms_offer(client: AsyncClient) -> None:
    headers = (await register(client)).headers

    options = (await client.get(f"{TRAVEL}/options", headers=headers)).json()

    assert options["activity_kinds"] == list(rules().activities.kinds)
    assert options["max_trip_days"] == rules().trips.max_days


async def test_one_person_never_sees_anothers_trips(
    client: AsyncClient, other_client: AsyncClient
) -> None:
    owner, intruder = await register(client), await register(other_client)
    await add_trip(client, owner)

    listed = (await other_client.get(TRIPS, headers=intruder.headers)).json()

    assert listed["total"] == 0


def test_the_manifest_lists_every_document_travel_keeps() -> None:
    """So a person's export and erasure cover them: a record nobody lists would be left behind."""
    kept = {
        f"{models.__name__}:{name}"
        for name, thing in vars(models).items()
        if inspect.isclass(thing)
        and issubclass(thing, Document)
        and thing.__module__ == models.__name__
    }

    assert kept == set(MANIFEST.documents)


async def test_a_persons_export_and_erasure_cover_it_all(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account, travellers=[{"name": "Asha"}])
    activity = await add_activity(client, account, trip["id"])
    booking = await add_booking(client, account, trip["id"])
    expense = await add_expense(client, account, trip["id"])

    file = await client.get("/v1/me/export", headers=account.headers)
    held = json.loads(file.content)["collections"]
    assert [item["title"] for item in held["travel_trips"]] == ["Goa"]
    assert [item["title"] for item in held["travel_activities"]] == [activity["title"]]
    assert [item["title"] for item in held["travel_bookings"]] == [booking["title"]]
    assert [item["title"] for item in held["travel_expenses"]] == [expense["title"]]
    for revisions in ("trip", "activity", "booking", "expense"):
        assert held[f"travel_{revisions}_revisions"], revisions
    assert [item["kind"] for item in held["travel_events"]] == [
        "trip_created",
        "activity_added",
        "booking_added",
        "expense_added",
    ]

    erased = await client.request(
        "DELETE",
        "/v1/me",
        headers=account.headers,
        json={"confirm_email": account.email, "password": account.password},
    )

    assert erased.status_code in {200, 204}, erased.text
    assert all(not rows for rows in (await stored(account)).values())
