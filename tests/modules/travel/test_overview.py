"""A trip's overview: readiness only from real tasks (rule 1), the next action from an ordered rule
list with its reason (rule 2), the countdown and what is next and open. And Travel outside its own
pages: the Welcome tile, the dashboard's next trip, the command palette and the activity feed."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.modules.travel import overview
from app.modules.travel.models import TripPhase
from app.modules.travel.overview import TripState, next_action
from app.modules.travel.rules import NextAction, OverviewRules, rules
from app.modules.travel.widgets import travel_next_trip
from app.platform import DateRange
from tests.helpers import Account, actor_of, register
from tests.modules.travel.conftest import (
    BOOKINGS,
    FIRST_DAY,
    LAST_DAY,
    SECOND_DAY,
    TRIPS,
    add_activity,
    add_booking,
    add_item,
    add_trip,
    change,
    change_item,
)


async def overview_of(client: AsyncClient, account: Account, trip_id: str) -> dict[str, Any]:
    response = await client.get(f"{TRIPS}/{trip_id}/overview", headers=account.headers)
    assert response.status_code == 200, response.text
    return dict(response.json())


async def today_of(app: FastAPI, account: Account) -> dt.date:
    return (await actor_of(app, account)).today()


def days(today: dt.date, first: int, last: int) -> dict[str, str]:
    return {
        "start_date": (today + dt.timedelta(days=first)).isoformat(),
        "end_date": (today + dt.timedelta(days=last)).isoformat(),
    }


async def item_in(
    client: AsyncClient, account: Account, trip_id: str, state: str, **fields: Any
) -> dict[str, Any]:
    item = await add_item(client, account, trip_id, **fields)
    if state != "not_started":
        assert (await change_item(client, account, item["id"], state=state)).is_success
    return item


# ---------------------------------------------------------------------- readiness (rule 1)


async def test_with_no_tasks_there_is_no_readiness_figure(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    assert (await overview_of(client, account, trip["id"]))["readiness"] is None

    for title in ("Visa", "Adapter"):
        await item_in(client, account, trip["id"], "not_applicable", title=title)

    assert (await overview_of(client, account, trip["id"]))["readiness"] is None  # none apply


async def test_readiness_is_what_is_completed_of_what_still_applies(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    for state, bag in [
        ("completed", None),
        ("completed", "cabin"),
        ("in_progress", None),
        ("not_started", "check_in"),
        ("not_applicable", None),
    ]:
        await item_in(client, account, trip["id"], state, bag=bag)

    found = await overview_of(client, account, trip["id"])

    assert found["readiness"] == {"completed": 2, "applicable": 4, "ratio": 0.5}
    assert found["pending"] == {"checklist": 1, "packing": 1, "bookings": 0}


# ---------------------------------------------------------------------- countdown and what's next


async def test_an_upcoming_trip_counts_down_and_shows_what_comes_first(
    app: FastAPI, client: AsyncClient
) -> None:
    account = await register(client)
    today = await today_of(app, account)
    trip = await add_trip(client, account, **days(today, 10, 13))
    first, second = trip["start_date"], (today + dt.timedelta(days=11)).isoformat()
    await add_activity(client, account, trip["id"], title="Later", day=second)
    skipped = await add_activity(client, account, trip["id"], title="Skipped", day=first)
    await change(client, account, skipped["id"], action="skip", apply=True)
    for title in ("One", "Two"):
        await add_activity(client, account, trip["id"], title=title, day=first)
    await add_activity(client, account, trip["id"], title="Last", day=second)

    found = await overview_of(client, account, trip["id"])

    assert (found["today"], found["phase"]) == (today.isoformat(), "upcoming")
    assert (found["days_to_start"], found["day_number"], found["trip_days"]) == (10, None, 4)
    assert [item["title"] for item in found["next_activities"]] == ["One", "Two", "Later"]
    assert len(found["next_activities"]) == rules().overview.next_activities


async def test_a_trip_under_way_says_which_day_it_is(app: FastAPI, client: AsyncClient) -> None:
    account = await register(client)
    today = await today_of(app, account)
    trip = await add_trip(client, account, **days(today, -1, 2))
    await add_activity(client, account, trip["id"], title="Gone by", day=trip["start_date"])
    await add_activity(client, account, trip["id"], title="Now", day=today.isoformat())

    found = await overview_of(client, account, trip["id"])

    assert (found["phase"], found["days_to_start"], found["day_number"]) == ("active", -1, 2)
    assert [item["title"] for item in found["next_activities"]] == ["Now"]


async def test_bookings_not_yet_confirmed_are_pending(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    await add_booking(client, account, trip["id"])
    confirmed = await add_booking(client, account, trip["id"], title="Hotel", status="confirmed")
    assert confirmed["status"] == "confirmed"

    found = await overview_of(client, account, trip["id"])

    assert found["pending"]["bookings"] == 1


# ---------------------------------------------------------------------- the next action (rule 2)


async def test_the_next_action_follows_the_trips_own_records(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    trip_id = trip["id"]

    async def next_one() -> tuple[str, str, dict[str, Any]]:
        found = (await overview_of(client, account, trip_id))["next_action"]
        return found["action"], found["reason"], found["params"]

    assert await next_one() == ("add_activities", "no_activities", {})
    await add_activity(client, account, trip_id)
    assert await next_one() == ("start_packing", "no_packing", {})
    booking = await add_booking(client, account, trip_id)
    assert await next_one() == ("confirm_bookings", "bookings_unconfirmed", {"count": 1})
    await client.patch(
        f"{BOOKINGS}/{booking['id']}",
        headers=account.headers,
        json={"status": "confirmed", "revision_no": 1},
    )
    packing = await add_item(client, account, trip_id, title="Shirts", bag="check_in")
    assert await next_one() == ("finish_packing", "packing_open", {"open": 1})
    await change_item(client, account, packing["id"], state="completed")
    assert await next_one() == ("all_set", "nothing_open", {})
    await add_item(client, account, trip_id, title="Book a cab", day=SECOND_DAY)
    assert await next_one() == (
        "finish_checklist",
        "checklist_open",
        {"open": 1, "day": SECOND_DAY},
    )


async def test_finishing_the_checklist_opens_the_first_day_still_holding_something_to_do(
    client: AsyncClient,
) -> None:
    """So "Do it now" opens a day whose plan shows what is open; with only the whole trip's open,
    there is no day to open."""
    account = await register(client)
    trip = await add_trip(client, account)
    await add_activity(client, account, trip["id"])
    await item_in(client, account, trip["id"], "completed", title="Shirts", bag="check_in")
    await add_item(client, account, trip["id"], title="Visa")

    def finishing(found: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        return found["next_action"]["action"], found["next_action"]["params"]

    found = await overview_of(client, account, trip["id"])
    assert finishing(found) == ("finish_checklist", {"open": 1, "day": None})

    await item_in(client, account, trip["id"], "completed", title="Done", day=FIRST_DAY)
    await add_item(client, account, trip["id"], title="Cab", day=LAST_DAY)
    await item_in(client, account, trip["id"], "in_progress", title="Tickets", day=SECOND_DAY)

    found = await overview_of(client, account, trip["id"])
    assert finishing(found) == ("finish_checklist", {"open": 3, "day": SECOND_DAY})


async def test_on_the_way_today_and_tomorrow_come_first(app: FastAPI, client: AsyncClient) -> None:
    account = await register(client)
    today = await today_of(app, account)
    trip = await add_trip(client, account, **days(today, 0, 3))
    tomorrow = (today + dt.timedelta(days=1)).isoformat()
    await add_activity(client, account, trip["id"], day=today.isoformat())
    await add_booking(client, account, trip["id"])  # not confirmed: but the day comes first
    await add_item(client, account, trip["id"], title="Tickets", day=tomorrow)
    assert (await overview_of(client, account, trip["id"]))["next_action"] == {
        "action": "prepare_tomorrow",
        "reason": "open_tomorrow",
        "params": {"day": tomorrow, "open": 1},
    }

    await add_item(client, account, trip["id"], title="Sunscreen", day=today.isoformat())

    assert (await overview_of(client, account, trip["id"]))["next_action"] == {
        "action": "prepare_today",
        "reason": "open_today",
        "params": {"day": today.isoformat(), "open": 1},
    }


async def test_a_trip_with_no_tasks_at_all_is_never_called_ready(
    app: FastAPI, client: AsyncClient
) -> None:
    """Under way, with a plan but nothing to do or pack: not "all set", only no tasks yet."""
    account = await register(client)
    today = await today_of(app, account)
    trip = await add_trip(client, account, **days(today, 0, 3))
    await add_activity(client, account, trip["id"], day=today.isoformat())

    found = await overview_of(client, account, trip["id"])

    assert found["readiness"] is None
    assert found["next_action"] == {"action": "add_tasks", "reason": "no_tasks", "params": {}}
    item = await add_item(client, account, trip["id"], title="Tickets", bag="cabin")
    await change_item(client, account, item["id"], state="completed")
    assert (await overview_of(client, account, trip["id"]))["next_action"]["action"] == "all_set"


async def test_a_trip_gone_by_says_so_whatever_is_left_open(
    app: FastAPI, client: AsyncClient
) -> None:
    account = await register(client)
    today = await today_of(app, account)
    trip = await add_trip(client, account, **days(today, -6, -2))
    await add_booking(client, account, trip["id"])
    await add_item(client, account, trip["id"], title="Never ticked", bag="cabin")

    found = await overview_of(client, account, trip["id"])

    assert (found["phase"], found["next_action"]["action"]) == ("past", "trip_ended")
    assert found["next_action"]["params"] == {"end_date": trip["end_date"]}


def state(**fields: Any) -> TripState:
    quiet: dict[str, Any] = {
        "phase": TripPhase.UPCOMING,
        "end_date": LAST_DAY,
        "today": "2030-02-20",
        "tomorrow": None,
        "activities": 1,
        "open_today": 0,
        "open_tomorrow": 0,
        "bookings_to_confirm": 0,
        "packing": 1,
        "packing_open": 0,
        "checklist_open": 0,
        "tasks": 1,
    }
    return TripState(**{**quiet, **fields})


def test_the_first_rule_that_applies_in_the_datas_order_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    busy = state(bookings_to_confirm=2, checklist_open=3)
    assert next_action(busy).action is NextAction.CONFIRM_BOOKINGS
    assert next_action(busy) == next_action(busy)  # the same records, the same answer
    assert next_action(state()).action is NextAction.ALL_SET

    reordered = OverviewRules(
        next_activities=3,
        next_actions=(NextAction.FINISH_CHECKLIST, NextAction.CONFIRM_BOOKINGS, NextAction.ALL_SET),
    )
    monkeypatch.setattr(
        overview, "rules", lambda: rules().model_copy(update={"overview": reordered})
    )

    found = next_action(busy)
    assert (found.action, found.reason, found.params) == (
        NextAction.FINISH_CHECKLIST,
        "checklist_open",
        {"open": 3, "day": None},
    )


def test_every_next_action_has_a_rule_and_the_list_always_ends_with_one_that_applies() -> None:
    assert set(overview.RULES) == set(NextAction)
    assert set(rules().overview.next_actions) == set(NextAction)
    for broken in [
        (NextAction.ALL_SET, NextAction.TRIP_ENDED),
        (NextAction.TRIP_ENDED, NextAction.TRIP_ENDED, NextAction.ALL_SET),
    ]:
        with pytest.raises(ValueError, match=r"next action|all_set"):
            OverviewRules(next_activities=1, next_actions=broken)


# ---------------------------------------------------------------------- outside the module


async def test_the_tile_counts_the_trips_to_come_and_under_way(
    app: FastAPI, client: AsyncClient
) -> None:
    account = await register(client)
    today = await today_of(app, account)
    for first, last in [(5, 8), (-1, 1), (-9, -7)]:  # ahead, under way, gone by
        await add_trip(client, account, **days(today, first, last))

    view = (await client.get("/v1/modules/travel", headers=account.headers)).json()

    assert view["stat"] == {"value": 2, "format": "number", "currency": None, "label": "items"}


async def test_the_dashboard_shows_the_trip_under_way_else_the_next_one(
    app: FastAPI, client: AsyncClient
) -> None:
    account = await register(client)
    ctx = await actor_of(app, account)
    today = ctx.today()
    week = DateRange(today - dt.timedelta(days=6), today, ctx.user.timezone)
    assert (await travel_next_trip(ctx, week)).items == []

    await add_trip(client, account, title="Later", **days(today, 30, 32))
    await add_trip(client, account, title="Sooner", **days(today, 9, 12))
    await add_trip(client, account, title="Gone by", **days(today, -9, -7))
    [ahead] = (await travel_next_trip(ctx, week)).items
    assert (ahead.code, ahead.params["title"]) == ("travel.trip_upcoming", "Sooner")

    now = await add_trip(client, account, title="Now", **days(today, -1, 1))
    [under_way] = (await travel_next_trip(ctx, week)).items
    assert under_way.code == "travel.trip_active"
    assert under_way.params == {
        "module": "travel",
        "title": "Now",
        "trip_id": now["id"],
        "start": now["start_date"],
        "end": now["end_date"],
    }


async def test_the_command_palette_finds_trips_by_title(
    client: AsyncClient, other_client: AsyncClient
) -> None:
    owner, intruder = await register(client), await register(other_client)
    trip = await add_trip(client, owner, title="Honeymoon in Goa")
    await add_trip(client, owner, title="Kerala")

    found = (await client.get("/v1/search?q=goa", headers=owner.headers)).json()
    theirs = (await other_client.get("/v1/search?q=goa", headers=intruder.headers)).json()

    [hit] = [
        hit for group in found["groups"] for hit in group["hits"] if group["owner"] == "travel"
    ]
    assert (hit["kind"], hit["title"], hit["path"], hit["date"]) == (
        "trip",
        "Honeymoon in Goa",
        f"trips/{trip['id']}",
        FIRST_DAY,
    )
    assert all(group["owner"] != "travel" for group in theirs["groups"])


async def test_the_activity_feed_notes_trips_bookings_and_expenses(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    url = f"{TRIPS}/{trip['id']}"
    await client.patch(url, headers=account.headers, json={"mode": "basic", "revision_no": 1})
    await client.patch(url, headers=account.headers, json={"title": "Kerala", "revision_no": 2})
    await add_booking(client, account, trip["id"])
    await client.post(
        f"{url}/expenses",
        headers=account.headers,
        json={"kind": "estimate", "category": "stay", "title": "Hotel", "amount_minor": 1},
    )
    await client.delete(url, headers=account.headers)
    await client.post(f"{url}/undelete", headers=account.headers)

    feed = (await client.get("/v1/activity", headers=account.headers)).json()["items"]

    mine = [item for item in reversed(feed) if item["type"].startswith("travel.")]
    assert [(item["type"], item["params"]) for item in mine] == [
        ("travel.trip_added", {"module": "travel", "title": "Goa"}),  # a mode switch isn't noted
        ("travel.trip_updated", {"module": "travel", "title": "Kerala"}),
        ("travel.booking_added", {"module": "travel", "title": "Island tour", "trip": "Kerala"}),
        ("travel.expense_added", {"module": "travel", "title": "Hotel", "trip": "Kerala"}),
        ("travel.trip_deleted", {"module": "travel", "title": "Kerala"}),
        ("travel.trip_restored", {"module": "travel", "title": "Kerala"}),
    ]
