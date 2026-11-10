"""One day's plan, for Get Me Ready and Prepare Tomorrow: that day's activities, checklist and
transport, on the trip's own calendar, and an honest empty answer for a day with nothing on it;
and which of the trip's days still hold items to do. That day's bookings are tested with the
bookings (test_bookings.py)."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response

from tests.helpers import Account, FrozenClock, actor_of, register
from tests.modules.travel.conftest import (
    FIRST_DAY,
    LAST_DAY,
    SECOND_DAY,
    TRIPS,
    add_activity,
    add_item,
    add_trip,
    change,
    change_item,
    stored,
)

WEST, EAST = "Pacific/Pago_Pago", "Pacific/Kiritimati"  # 25 hours apart: never the same day


async def prepare(
    client: AsyncClient, account: Account, trip_id: str, when: str | None = None
) -> Response:
    query = f"?when={when}" if when else ""
    return await client.get(f"{TRIPS}/{trip_id}/prepare{query}", headers=account.headers)


async def test_a_day_has_its_activities_checklist_and_transport(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    trip_id = trip["id"]
    on_day = {"day": SECOND_DAY}
    flight = await add_activity(
        client, account, trip_id, title="Fly in", kind="flight", start_time="06:30", **on_day
    )
    beach = await add_activity(
        client, account, trip_id, title="Beach", transport="Taxi", location="Baga", **on_day
    )
    await add_activity(client, account, trip_id, title="Nap", kind="rest", **on_day)
    ferry = await add_activity(client, account, trip_id, title="Ferry", kind="transport", **on_day)
    assert (await change(client, account, ferry["id"], action="skip", apply=True)).is_success
    await add_activity(client, account, trip_id, title="Elsewhere", kind="flight", day=FIRST_DAY)
    tied = await add_item(client, account, trip_id, title="Towel", activity_id=beach["id"])
    loose = await add_item(client, account, trip_id, title="Cash", **on_day)
    await add_item(client, account, trip_id, title="Other day", day=FIRST_DAY)
    await add_item(client, account, trip_id, title="Any day")

    answer = await prepare(client, account, trip_id, SECOND_DAY)

    assert answer.status_code == 200, answer.text
    plan = answer.json()
    assert (plan["day"], plan["within_trip"]) == (SECOND_DAY, True)
    assert [item["title"] for item in plan["activities"]] == ["Fly in", "Beach", "Nap", "Ferry"]
    assert plan["activities"][3]["status"] == "skipped"  # shown as it stands, not hidden
    assert [item["id"] for item in plan["checklist"]] == [tied["id"], loose["id"]]
    # Only what the person wrote, and nothing off the plan: no skipped ferry, no nap.
    assert plan["transport"] == [
        {
            "activity_id": flight["id"],
            "title": "Fly in",
            "kind": "flight",
            "start_time": "06:30",
            "location": None,
            "transport": None,
        },
        {
            "activity_id": beach["id"],
            "title": "Beach",
            "kind": "beach",
            "start_time": None,
            "location": "Baga",
            "transport": "Taxi",
        },
    ]
    assert plan["bookings"] == []  # none entered (tests/modules/travel/test_bookings.py)
    assert plan["open_days"] == [FIRST_DAY, SECOND_DAY]
    assert set(plan) == {  # no weather
        "day",
        "within_trip",
        "activities",
        "checklist",
        "bookings",
        "transport",
        "open_days",
    }


@pytest.mark.parametrize("day", [SECOND_DAY, "2030-04-01"], ids=["in the trip", "after it"])
async def test_a_day_with_nothing_on_it_is_an_honest_empty_answer(
    client: AsyncClient, day: str
) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    await add_activity(client, account, trip["id"])  # on the first day
    await add_item(client, account, trip["id"], title="Any day")
    before = await stored(account)

    answer = await prepare(client, account, trip["id"], day)

    assert answer.status_code == 200, answer.text
    assert answer.json() == {
        "day": day,
        "within_trip": day <= LAST_DAY,
        "activities": [],
        "checklist": [],
        "bookings": [],
        "transport": [],
        "open_days": [],  # the whole trip's item is no day's
    }
    assert await stored(account) == before  # reading a day writes nothing


async def test_any_day_says_which_days_still_hold_something_to_do(client: AsyncClient) -> None:
    """So the page can open the day an open item is on: packing ones too, as a day's plan lists
    them, and never a day whose items are all done or no longer apply."""
    account = await register(client)
    trip = await add_trip(client, account)
    await add_item(client, account, trip["id"], title="Started", day=LAST_DAY, state="in_progress")
    await add_item(client, account, trip["id"], title="Cabin bag", day=SECOND_DAY, bag="cabin")
    done = await add_item(client, account, trip["id"], title="Done", day=FIRST_DAY)
    await change_item(client, account, done["id"], state="completed")
    await add_item(client, account, trip["id"], title="Gone", day=FIRST_DAY, state="not_applicable")
    await add_item(client, account, trip["id"], title="Whenever")

    for when in (FIRST_DAY, "2030-04-01"):
        answer = (await prepare(client, account, trip["id"], when)).json()
        assert answer["open_days"] == [SECOND_DAY, LAST_DAY], when


@pytest.mark.parametrize("zone", [EAST, WEST])
async def test_today_and_tomorrow_are_on_the_trips_own_calendar(
    client: AsyncClient, clock: FrozenClock, zone: str
) -> None:
    account = await register(client)
    today = clock.now().astimezone(ZoneInfo(zone)).date()
    tomorrow = (today + dt.timedelta(days=1)).isoformat()
    place = {"name": "Far", "country": "Away", "timezone": zone}
    trip = await add_trip(
        client, account, destinations=[place], start_date=today.isoformat(), end_date=tomorrow
    )
    await add_activity(client, account, trip["id"], title="Now", day=today.isoformat())
    await add_activity(client, account, trip["id"], title="Next", day=tomorrow)

    plain = (await prepare(client, account, trip["id"])).json()
    now = (await prepare(client, account, trip["id"], "today")).json()
    later = (await prepare(client, account, trip["id"], "tomorrow")).json()

    assert plain == now
    assert (now["day"], [item["title"] for item in now["activities"]]) == (
        today.isoformat(),
        ["Now"],
    )
    assert (later["day"], [item["title"] for item in later["activities"]]) == (tomorrow, ["Next"])


async def test_without_a_time_zone_today_is_the_persons_own(
    app: FastAPI, client: AsyncClient
) -> None:
    account = await register(client)
    today = (await actor_of(app, account)).today().isoformat()
    trip = await add_trip(client, account, start_date=today, end_date=today)

    plan = (await prepare(client, account, trip["id"])).json()

    assert (plan["day"], plan["within_trip"]) == (today, True)


@pytest.mark.parametrize("when", ["yesterday", "2030-13-01", "01-03-2030"])
async def test_when_is_today_tomorrow_or_a_date(client: AsyncClient, when: str) -> None:
    account = await register(client)
    trip = await add_trip(client, account)

    response = await prepare(client, account, trip["id"], when)

    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_ERROR")


async def test_a_deleted_trips_days_are_out_of_reach(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    await client.delete(f"{TRIPS}/{trip['id']}", headers=account.headers)

    response = await prepare(client, account, trip["id"], FIRST_DAY)

    assert response.status_code == 404
