"""The one reminder a trip can ask for (behaviour rule 8): the day before it starts, through
Reminders. It moves with the dates, goes with the setting or the trip and comes back with them, an
undo's too; and a mode switch leaves it, like the checklist, as it is (rule 7)."""

from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from httpx import AsyncClient

from tests.helpers import Account, FrozenClock, actor_of, register
from tests.modules.travel.conftest import (
    FIRST_DAY,
    SECOND_DAY,
    TRIPS,
    add_activity,
    add_item,
    add_trip,
    history,
    stored,
    suggest,
    undo,
)

PREPARE_DUE = "travel.prepare_due"


async def reminders(client: AsyncClient, account: Account) -> list[dict[str, Any]]:
    """The person's open Travel reminders (listing them first tells any that are due)."""
    page = (await client.get("/v1/reminders", headers=account.headers)).json()
    return [item for item in page["items"] if item["type"] == PREPARE_DUE]


async def due_days(client: AsyncClient, account: Account) -> list[str]:
    return [item["due_on"] for item in await reminders(client, account)]


async def edit_trip(
    client: AsyncClient, account: Account, trip_id: str, **fields: Any
) -> dict[str, Any]:
    url = f"{TRIPS}/{trip_id}"
    revision = (await client.get(url, headers=account.headers)).json()["revision_no"]
    response = await client.patch(
        url, headers=account.headers, json={**fields, "revision_no": revision}
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


async def test_a_trip_that_asks_is_reminded_the_day_before_it_starts(
    app: FastAPI, client: AsyncClient
) -> None:
    account = await register(client)
    zone = ZoneInfo((await actor_of(app, account)).user.timezone)
    await add_trip(client, account, title="Not asked")
    trip = await add_trip(client, account, title="Goa", remind_prepare=True)

    [reminder] = await reminders(client, account)

    assert reminder["due_on"] == FIRST_DAY
    assert (reminder["params"]["trip_id"], reminder["params"]["title"]) == (trip["id"], "Goa")
    told_on = dt.datetime.fromisoformat(reminder["remind_at"]).astimezone(zone).date()
    assert told_on == dt.date.fromisoformat(FIRST_DAY) - dt.timedelta(days=1)
    assert reminder["sent"] is False


async def test_it_moves_with_the_dates_and_follows_the_setting(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account, remind_prepare=True)

    await edit_trip(client, account, trip["id"], start_date=SECOND_DAY)
    assert await due_days(client, account) == [SECOND_DAY]

    await edit_trip(client, account, trip["id"], remind_prepare=False)
    assert await due_days(client, account) == []

    await edit_trip(client, account, trip["id"], remind_prepare=True)
    assert await due_days(client, account) == [SECOND_DAY]

    await edit_trip(client, account, trip["id"], title="Kerala")
    assert [item["params"]["title"] for item in await reminders(client, account)] == ["Kerala"]


async def test_a_refused_change_leaves_it_where_it_was(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account, remind_prepare=True)
    await add_activity(client, account, trip["id"])  # on the first day

    refused = await client.patch(
        f"{TRIPS}/{trip['id']}",
        headers=account.headers,
        json={"start_date": SECOND_DAY, "revision_no": 1},
    )

    assert (refused.status_code, refused.json()["code"]) == (409, "TRAVEL_ACTIVITIES_OUTSIDE_DATES")
    assert await due_days(client, account) == [FIRST_DAY]


async def test_it_goes_with_the_trip_and_comes_back_with_it(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account, remind_prepare=True)
    url = f"{TRIPS}/{trip['id']}"

    assert (await client.delete(url, headers=account.headers)).status_code == 204
    assert await due_days(client, account) == []

    assert (await client.post(f"{url}/undelete", headers=account.headers)).status_code == 200
    assert await due_days(client, account) == [FIRST_DAY]


async def test_an_undo_takes_the_reminder_back_with_the_trip(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account, remind_prepare=True)

    await edit_trip(client, account, trip["id"], start_date=SECOND_DAY)
    moved = (await history(client, account, trip["id"]))[0]
    assert (await undo(client, account, moved["id"])).status_code == 200
    assert await due_days(client, account) == [FIRST_DAY]

    await edit_trip(client, account, trip["id"], remind_prepare=False)
    switched_off = (await history(client, account, trip["id"]))[0]
    assert await due_days(client, account) == []
    assert (await undo(client, account, switched_off["id"])).status_code == 200
    assert await due_days(client, account) == [FIRST_DAY]


async def test_a_trip_starting_today_has_no_day_before_left(
    app: FastAPI, client: AsyncClient
) -> None:
    account = await register(client)
    today = (await actor_of(app, account)).today()
    later = (today + dt.timedelta(days=2)).isoformat()

    await add_trip(
        client, account, remind_prepare=True, start_date=today.isoformat(), end_date=later
    )

    assert await due_days(client, account) == []


async def test_the_reminder_is_told_on_the_day_before(
    app: FastAPI, client: AsyncClient, clock: FrozenClock
) -> None:
    account = await register(client)
    start = ((await actor_of(app, account)).today() + dt.timedelta(days=3)).isoformat()
    trip = await add_trip(client, account, remind_prepare=True, start_date=start, end_date=start)
    assert [item["sent"] for item in await reminders(client, account)] == [False]

    clock.advance(days=3)  # past the send hour of the day before, wherever the person is

    assert [item["sent"] for item in await reminders(client, account)] == [True]
    page = (await client.get("/v1/notifications", headers=account.headers)).json()
    told = [item for item in page["items"] if item["type"] == PREPARE_DUE]
    assert [(item["params"]["trip_id"], item["params"]["due_on"]) for item in told] == [
        (trip["id"], start)
    ]


async def test_a_mode_switch_leaves_the_checklist_and_the_reminder_as_they_are(
    client: AsyncClient,
) -> None:
    account = await register(client)
    trip = await add_trip(client, account, remind_prepare=True)
    activity = await add_activity(client, account, trip["id"])
    await add_item(client, account, trip["id"], activity_id=activity["id"])
    assert (await suggest(client, account, trip["id"], packing=True, apply=True)).is_success
    before, held = await stored(account), await reminders(client, account)

    switched = await client.patch(
        f"{TRIPS}/{trip['id']}", headers=account.headers, json={"mode": "basic", "revision_no": 1}
    )

    assert (switched.status_code, switched.json()["mode"]) == (200, "basic")
    after = await stored(account)
    for name in (
        "travel_checklist",
        "travel_checklist_revisions",
        "travel_activities",
        "travel_activity_revisions",
        "travel_events",
    ):
        assert after[name] == before[name], name
    assert await reminders(client, account) == held
