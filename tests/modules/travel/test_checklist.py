"""Checklist and packing items: one store, ids in a body checked against the same trip, an edit
makes an item the person's own, a tick stays out of the trip's history, and a change to an activity
reaches only what is tied to it, with an undo that puts it back (behaviour rules 3, 5 and 6)."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from app.modules.travel.rules import rules
from tests.helpers import Account, register
from tests.modules.travel.conftest import (
    ACTIVITIES,
    CHECKLIST,
    FIRST_DAY,
    LAST_DAY,
    SECOND_DAY,
    TRIPS,
    add_activity,
    add_item,
    add_trip,
    change,
    change_item,
    history,
    items_of,
    newest_event,
    read_activity,
    read_item,
    stored,
    undo,
)


async def tied_item(
    client: AsyncClient, account: Account, **fields: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    """An activity on the first day, and an item tied to it."""
    trip = await add_trip(client, account)
    activity = await add_activity(client, account, trip["id"])
    item = await add_item(client, account, trip["id"], activity_id=activity["id"], **fields)
    return activity, item


# ---------------------------------------------------------------------- the routes


async def test_an_item_is_added_changed_and_removed(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account, travellers=[{"name": "Asha"}])
    asha = trip["travellers"][0]["id"]

    item = await add_item(
        client,
        account,
        trip["id"],
        title="  Swimwear  ",
        day=SECOND_DAY,
        traveller_id=asha,
        bag="day_bag",
        quantity=2,
    )

    assert item["title"] == "Swimwear"
    assert (item["state"], item["source"], item["edited"]) == ("not_started", "user", False)
    assert (item["day"], item["bag"], item["quantity"]) == (SECOND_DAY, "day_bag", 2)
    assert (item["traveller_id"], item["suggestion_key"]) == (asha, None)
    assert "dismissed" not in item

    changed = await change_item(client, account, item["id"], title="Two swimsuits", revision_no=1)
    assert changed.status_code == 200, changed.text
    now = changed.json()
    assert (now["title"], now["edited"], now["revision_no"]) == ("Two swimsuits", True, 2)
    assert now["bag"] == "day_bag"  # only what was sent

    url = f"{CHECKLIST}/{item['id']}"
    removed = await client.delete(url, headers=account.headers)
    assert removed.status_code == 200, removed.text
    assert removed.json() == {"event_id": await newest_event(client, account, trip["id"])}
    assert await items_of(client, account, trip["id"]) == []
    assert (await client.delete(url, headers=account.headers)).status_code == 404
    assert [event["kind"] for event in await history(client, account, trip["id"])] == [
        "checklist_removed",
        "checklist_edited",
        "checklist_added",
        "trip_created",
    ]


async def test_an_item_is_kept_once_per_idempotency_key(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    headers = {**account.headers, "Idempotency-Key": "item-key-0001"}
    url = f"{TRIPS}/{trip['id']}/checklist"

    first = await client.post(url, headers=headers, json={"title": "Towel"})
    second = await client.post(url, headers=headers, json={"title": "Again"})

    assert (first.status_code, second.status_code) == (201, 201)
    assert second.json()["id"] == first.json()["id"]
    assert second.headers["Idempotent-Replayed"] == "true"
    assert [item["title"] for item in await items_of(client, account, trip["id"])] == ["Towel"]
    kinds = [event["kind"] for event in await history(client, account, trip["id"])]
    assert kinds.count("checklist_added") == 1


async def test_the_list_filters_by_day_bag_and_activity(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    activity = await add_activity(client, account, trip["id"], day=SECOND_DAY)
    other_trip = await add_trip(client, account)
    await add_item(client, account, other_trip["id"], title="Elsewhere", day=SECOND_DAY)
    await add_item(client, account, trip["id"], title="Any day")
    await add_item(client, account, trip["id"], title="First", day=FIRST_DAY, bag="cabin")
    await add_item(client, account, trip["id"], title="Second", day=SECOND_DAY, bag="cabin")
    await add_item(client, account, trip["id"], title="Tied", activity_id=activity["id"])

    async def titles(query: str = "") -> list[str]:
        return [item["title"] for item in await items_of(client, account, trip["id"], query)]

    assert await titles() == ["Any day", "First", "Second", "Tied"]  # no day first, then by day
    assert await titles(f"?day={SECOND_DAY}") == ["Second", "Tied"]
    assert await titles("?bag=cabin") == ["First", "Second"]
    assert await titles(f"?day={FIRST_DAY}&bag=cabin") == ["First"]
    assert await titles(f"?activity_id={activity['id']}") == ["Tied"]
    assert await titles("?activity_id=nope") == []
    wrong = await client.get(
        f"{TRIPS}/{trip['id']}/checklist?bag=rucksack", headers=account.headers
    )
    assert wrong.status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"title": ""},
        {"title": "x", "state": "lost"},
        {"title": "x", "source": "rule"},
        {"title": "x", "edited": True},
        {"title": "x", "suggestion_key": "packing:tops"},
        {"title": "x", "bag": "rucksack"},
        {"title": "x", "quantity": 0},
        {"title": "x", "quantity": rules().checklist.max_quantity + 1},
        {"title": "x", "day": "01-03-2030"},
    ],
)
async def test_what_an_item_can_not_be(client: AsyncClient, body: dict[str, Any]) -> None:
    account = await register(client)
    trip = await add_trip(client, account)

    response = await client.post(
        f"{TRIPS}/{trip['id']}/checklist", headers=account.headers, json=body
    )

    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_ERROR")


@pytest.mark.parametrize("field", ["title", "state", "quantity"])
async def test_a_required_field_can_not_be_cleared(client: AsyncClient, field: str) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    item = await add_item(client, account, trip["id"])

    response = await change_item(client, account, item["id"], **{field: None})

    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_ERROR")


async def test_a_trip_holds_as_many_items_as_the_rule(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    limits = rules().checklist.model_copy(update={"max_per_trip": 1})
    monkeypatch.setattr(  # the cap is held by the item's store, for every write
        "app.modules.travel.events.rules",
        lambda: rules().model_copy(update={"checklist": limits}),
    )
    await add_item(client, account, trip["id"])

    full = await client.post(
        f"{TRIPS}/{trip['id']}/checklist", headers=account.headers, json={"title": "One more"}
    )

    assert (full.status_code, full.json()["code"]) == (409, "TRAVEL_CHECKLIST_FULL")


# ---------------------------------------------------------------------- ids in a body (rule 6)


async def test_an_activity_named_must_be_on_the_same_trip(
    client: AsyncClient, other_client: AsyncClient
) -> None:
    account, stranger = await register(client), await register(other_client)
    trip = await add_trip(client, account)
    item = await add_item(client, account, trip["id"])
    other_trip = await add_trip(client, account)
    elsewhere = await add_activity(client, account, other_trip["id"])
    theirs = await add_activity(
        other_client, stranger, (await add_trip(other_client, stranger))["id"]
    )
    gone = await add_activity(client, account, trip["id"])
    await client.delete(f"{ACTIVITIES}/{gone['id']}", headers=account.headers)

    for named in (elsewhere["id"], theirs["id"], gone["id"], "0" * 24, "nope"):
        made = await client.post(
            f"{TRIPS}/{trip['id']}/checklist",
            headers=account.headers,
            json={"title": "x", "activity_id": named},
        )
        moved = await change_item(client, account, item["id"], activity_id=named)
        for response in (made, moved):
            assert (response.status_code, response.json()["code"]) == (
                422,
                "TRAVEL_ACTIVITY_UNKNOWN",
            )
            assert "details" not in response.json() or not response.json()["details"]


async def test_a_traveller_named_must_be_on_the_same_trip(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account, travellers=[{"name": "Asha"}])
    other = await add_trip(client, account, travellers=[{"name": "Ravi"}])
    item = await add_item(client, account, trip["id"])
    named = other["travellers"][0]["id"]

    made = await client.post(
        f"{TRIPS}/{trip['id']}/checklist",
        headers=account.headers,
        json={"title": "x", "traveller_id": named},
    )
    moved = await change_item(client, account, item["id"], traveller_id=named)

    for response in (made, moved):
        assert (response.status_code, response.json()["code"]) == (422, "TRAVEL_TRAVELLER_UNKNOWN")


async def test_a_traveller_with_items_stays_on_the_trip(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account, travellers=[{"name": "Asha"}, {"name": "Ravi"}])
    asha, ravi = trip["travellers"]
    await add_item(client, account, trip["id"], traveller_id=asha["id"])
    url = f"{TRIPS}/{trip['id']}"

    refused = await client.patch(
        url, headers=account.headers, json={"travellers": [ravi], "revision_no": 1}
    )
    allowed = await client.patch(
        url, headers=account.headers, json={"travellers": [asha], "revision_no": 1}
    )

    assert (refused.status_code, refused.json()["code"]) == (409, "TRAVEL_TRAVELLER_IN_USE")
    assert refused.json()["details"] == {"traveller_ids": [asha["id"]]}
    assert allowed.status_code == 200, allowed.text
    assert [person["name"] for person in allowed.json()["travellers"]] == ["Asha"]


# ---------------------------------------------------------------------- days


async def test_an_item_tied_to_an_activity_is_on_its_day(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    activity = await add_activity(client, account, trip["id"], day=SECOND_DAY)
    url = f"{TRIPS}/{trip['id']}/checklist"

    taken = await add_item(client, account, trip["id"], activity_id=activity["id"])
    named = await add_item(client, account, trip["id"], activity_id=activity["id"], day=SECOND_DAY)
    other_day = await client.post(
        url,
        headers=account.headers,
        json={"title": "x", "activity_id": activity["id"], "day": FIRST_DAY},
    )
    moved = await change_item(client, account, taken["id"], day=FIRST_DAY)
    free = await add_item(client, account, trip["id"], day=FIRST_DAY)
    tied_now = await change_item(client, account, free["id"], activity_id=activity["id"])

    assert (taken["day"], named["day"]) == (SECOND_DAY, SECOND_DAY)
    for response in (other_day, moved):
        assert (response.status_code, response.json()["code"]) == (
            422,
            "TRAVEL_CHECKLIST_DAY_MISMATCH",
        )
        assert response.json()["details"] == {"day": SECOND_DAY}
    assert tied_now.json()["day"] == SECOND_DAY  # tying it takes it to the activity's day


async def test_an_items_day_stays_within_the_trip(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    item = await add_item(client, account, trip["id"], day=FIRST_DAY)

    made = await client.post(
        f"{TRIPS}/{trip['id']}/checklist",
        headers=account.headers,
        json={"title": "x", "day": "2030-03-06"},
    )
    moved = await change_item(client, account, item["id"], day="2030-02-28")

    for response in (made, moved):
        assert (response.status_code, response.json()["code"]) == (422, "TRAVEL_DAY_OUTSIDE_TRIP")


async def test_a_trips_dates_hold_its_items_unless_they_no_longer_apply(
    client: AsyncClient,
) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    item = await add_item(client, account, trip["id"], day=LAST_DAY)
    url = f"{TRIPS}/{trip['id']}"

    refused = await client.patch(
        url, headers=account.headers, json={"end_date": SECOND_DAY, "revision_no": 1}
    )
    assert (refused.status_code, refused.json()["code"]) == (409, "TRAVEL_CHECKLIST_OUTSIDE_DATES")
    assert refused.json()["details"] == {"days": [LAST_DAY]}

    await change_item(client, account, item["id"], state="not_applicable")
    allowed = await client.patch(
        url, headers=account.headers, json={"end_date": SECOND_DAY, "revision_no": 1}
    )
    assert allowed.status_code == 200, allowed.text


# ---------------------------------------------------------------------- edits and ticks


async def test_a_stale_edit_is_refused_and_a_tick_needs_no_revision(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    item = await add_item(client, account, trip["id"])
    assert (await change_item(client, account, item["id"], title="A", revision_no=1)).is_success

    stale = await change_item(client, account, item["id"], title="B", revision_no=1)
    tick = await change_item(client, account, item["id"], state="completed")

    assert (stale.status_code, stale.json()["code"]) == (409, "VERSION_CONFLICT")
    assert stale.json()["details"] == {"current": 2}
    assert (tick.status_code, tick.json()["state"], tick.json()["title"]) == (200, "completed", "A")


async def test_a_tick_turns_edited_on_and_stays_out_of_the_history(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    item = await add_item(client, account, trip["id"])
    events = await history(client, account, trip["id"])

    for state in ("in_progress", "completed", "not_applicable", "not_started"):
        ticked = await change_item(client, account, item["id"], state=state)
        assert (ticked.status_code, ticked.json()["state"]) == (200, state), ticked.text
        assert ticked.json()["edited"] is True

    assert await history(client, account, trip["id"]) == events
    rows = (await stored(account))["travel_checklist_revisions"]
    assert [row["revision_no"] for row in rows] == [1, 2, 3, 4, 5]  # it keeps them itself
    assert rows[1]["changes"][0]["field"] in {"state", "edited"}


async def test_saving_nothing_new_changes_nothing(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    item = await add_item(client, account, trip["id"], title="Towel", bag="cabin", quantity=2)
    before = await stored(account)

    same = await change_item(client, account, item["id"], title="Towel", bag="cabin", quantity=2)

    assert (same.status_code, same.json()["edited"]) == (200, False)
    assert await stored(account) == before


async def test_an_edit_is_undone_with_its_edited_mark(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    item = await add_item(client, account, trip["id"], title="Towel")
    await change_item(client, account, item["id"], title="Big towel", state="completed")
    event = (await history(client, account, trip["id"]))[0]
    assert event["kind"] == "checklist_edited"
    assert {line["field"] for line in event["changes"][0]["fields"]} == {"title", "state"}

    assert (await undo(client, account, event["id"])).status_code == 200

    now = await read_item(client, account, item)
    assert (now["title"], now["state"], now["edited"]) == ("Towel", "not_started", False)


async def test_a_removed_item_comes_back_with_an_undo(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    item = await add_item(client, account, trip["id"], title="Towel")
    await client.delete(f"{CHECKLIST}/{item['id']}", headers=account.headers)
    event = (await history(client, account, trip["id"]))[0]
    assert event["kind"] == "checklist_removed"

    assert (await undo(client, account, event["id"])).status_code == 200

    assert (await read_item(client, account, item))["title"] == "Towel"


async def test_a_deleted_trips_items_are_out_of_reach(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    item = await add_item(client, account, trip["id"])
    await client.delete(f"{TRIPS}/{trip['id']}", headers=account.headers)

    listed = await client.get(f"{TRIPS}/{trip['id']}/checklist", headers=account.headers)
    changed = await change_item(client, account, item["id"], title="x")
    removed = await client.delete(f"{CHECKLIST}/{item['id']}", headers=account.headers)

    assert (listed.status_code, changed.status_code, removed.status_code) == (404, 404, 404)


# ---------------------------------------------------------------------- a scoped change (rule 3)


@pytest.mark.parametrize(
    "body",
    [
        {"action": "skip"},
        {"action": "replace", "new_activity": {"title": "Museum", "kind": "sightseeing"}},
    ],
    ids=lambda body: body["action"],
)
async def test_going_away_stands_down_what_is_untouched_and_keeps_the_persons_work(
    client: AsyncClient, body: dict[str, Any]
) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    activity = await add_activity(client, account, trip["id"])
    tied = {"activity_id": activity["id"]}
    untouched = await add_item(client, account, trip["id"], title="Untouched", **tied)
    edited = await add_item(client, account, trip["id"], title="Mine", **tied)
    await change_item(client, account, edited["id"], title="Mine, edited")
    done = await add_item(client, account, trip["id"], title="Done", state="completed", **tied)
    ticked = await add_item(client, account, trip["id"], title="Ticked", **tied)
    await change_item(client, account, ticked["id"], state="completed")
    apart = await add_item(client, account, trip["id"], title="Not tied", day=FIRST_DAY)
    edited_now = await read_item(client, account, edited)
    ticked_now = await read_item(client, account, ticked)

    preview = (await change(client, account, activity["id"], **body)).json()
    assert await read_item(client, account, untouched) == untouched  # a preview writes nothing
    answer = (await change(client, account, activity["id"], **body, apply=True)).json()

    now = await read_item(client, account, untouched)
    assert (now["state"], now["edited"]) == ("not_applicable", False)
    assert await read_item(client, account, edited) == edited_now
    assert await read_item(client, account, done) == done
    assert await read_item(client, account, ticked) == ticked_now
    assert await read_item(client, account, apart) == apart
    line = next(item for item in answer["what_changed"] if item["entity"] == "checklist")
    assert line["record_id"] == untouched["id"]
    assert (line["title"], line["day"]) == ("Untouched", FIRST_DAY)
    assert (line["fields"][0]["old"], line["fields"][0]["new"]) == ("not_started", "not_applicable")
    assert sorted((item["title"], item["reason"]) for item in answer["kept"]) == [
        ("Done", "completed"),
        ("Mine, edited", "edited"),
        ("Ticked", "completed"),
    ]
    assert preview["kept"] == answer["kept"]
    assert [item["title"] for item in preview["what_changed"]] == [
        item["title"] for item in answer["what_changed"]
    ]
    event = (await history(client, account, trip["id"]))[0]
    assert event["summary"]["kept"] == 3


async def test_marking_it_done_leaves_its_items_be(client: AsyncClient) -> None:
    account = await register(client)
    activity, item = await tied_item(client, account)

    answer = (await change(client, account, activity["id"], action="done", apply=True)).json()

    assert await read_item(client, account, item) == item
    assert [line["entity"] for line in answer["what_changed"]] == ["activity"]
    assert answer["kept"] == []


async def test_rescheduling_moves_every_tied_item_and_reports_both_days(
    client: AsyncClient,
) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    activity = await add_activity(client, account, trip["id"])
    tied = {"activity_id": activity["id"]}
    untouched = await add_item(client, account, trip["id"], title="Untouched", **tied)
    done = await add_item(client, account, trip["id"], title="Done", state="completed", **tied)
    edited = await add_item(client, account, trip["id"], title="Mine", **tied)
    await change_item(client, account, edited["id"], title="Mine, edited")
    apart = await add_item(client, account, trip["id"], title="Not tied", day=FIRST_DAY)

    answer = (
        await change(
            client, account, activity["id"], action="reschedule", new_day=LAST_DAY, apply=True
        )
    ).json()

    assert answer["days"] == [FIRST_DAY, LAST_DAY]
    for item in (untouched, done, edited):
        now = await read_item(client, account, item)
        assert now["day"] == LAST_DAY, item["title"]
    assert (await read_item(client, account, done))["state"] == "completed"
    assert await read_item(client, account, apart) == apart
    assert answer["kept"] == []
    moved = [line for line in answer["what_changed"] if line["entity"] == "checklist"]
    assert {line["title"] for line in moved} == {"Untouched", "Done", "Mine, edited"}


async def test_rescheduling_only_the_time_leaves_its_items_alone(client: AsyncClient) -> None:
    account = await register(client)
    activity, item = await tied_item(client, account)

    answer = (
        await change(
            client, account, activity["id"], action="reschedule", new_start_time="07:00", apply=True
        )
    ).json()

    assert await read_item(client, account, item) == item
    assert answer["days"] == [FIRST_DAY]
    assert [line["entity"] for line in answer["what_changed"]] == ["activity"]


@pytest.mark.parametrize(
    "body",
    [
        {"action": "skip"},
        {"action": "replace", "new_activity": {"title": "Museum", "kind": "sightseeing"}},
        {"action": "reschedule", "new_day": LAST_DAY},
    ],
    ids=lambda body: body["action"],
)
async def test_another_activitys_and_another_days_items_never_change(
    client: AsyncClient, body: dict[str, Any]
) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    activity = await add_activity(client, account, trip["id"])
    await add_item(client, account, trip["id"], title="Tied", activity_id=activity["id"])
    same_day = await add_activity(client, account, trip["id"], title="Same day")
    others = [
        await add_item(client, account, trip["id"], title="Same day", activity_id=same_day["id"]),
        await add_item(client, account, trip["id"], title="Other day", day=SECOND_DAY),
        await add_item(client, account, trip["id"], title="No day", bag="cabin"),
    ]
    ids = {item["id"] for item in others}

    def of_others(rows: dict[str, list[dict[str, Any]]]) -> list[list[dict[str, Any]]]:
        return [
            [row for row in rows["travel_checklist"] if str(row["_id"]) in ids],
            [row for row in rows["travel_checklist_revisions"] if str(row["entity_id"]) in ids],
        ]

    held = of_others(await stored(account))

    response = await change(client, account, activity["id"], **body, apply=True)

    assert response.status_code == 200, response.text
    assert of_others(await stored(account)) == held  # as stored, their histories too
    assert not ids & {line["record_id"] for line in response.json()["what_changed"]}


# ---------------------------------------------------------------------- undo with items (rule 5)


@pytest.mark.parametrize(
    "body",
    [
        {"action": "skip"},
        {"action": "replace", "new_activity": {"title": "Museum", "kind": "sightseeing"}},
        {"action": "reschedule", "new_day": LAST_DAY},
    ],
    ids=lambda body: body["action"],
)
async def test_an_undo_restores_the_items_it_changed(
    client: AsyncClient, body: dict[str, Any]
) -> None:
    account = await register(client)
    activity, item = await tied_item(client, account)
    answer = (await change(client, account, activity["id"], **body, apply=True)).json()
    assert await read_item(client, account, item) != item

    undone = await undo(client, account, answer["event_id"])

    assert undone.status_code == 200, undone.text
    now = await read_item(client, account, item)
    for name in ("day", "state", "edited", "title", "activity_id"):
        assert now[name] == item[name], name
    assert any(line["entity"] == "checklist" for line in undone.json()["changes"])


async def test_an_undo_is_refused_once_an_item_it_touched_has_changed(client: AsyncClient) -> None:
    account = await register(client)
    activity, item = await tied_item(client, account)
    answer = (await change(client, account, activity["id"], action="skip", apply=True)).json()
    ticked = await change_item(client, account, item["id"], state="in_progress")
    assert ticked.status_code == 200, ticked.text
    before = await stored(account)

    refused = await undo(client, account, answer["event_id"])

    assert (refused.status_code, refused.json()["code"]) == (409, "TRAVEL_UNDO_STALE")
    assert refused.json()["details"] == {"record_ids": [item["id"]]}
    assert await stored(account) == before  # nothing written, the activity still skipped
    assert (await read_activity(client, account, activity["id"]))["status"] == "skipped"


async def test_an_undo_doesnt_touch_a_kept_item(client: AsyncClient) -> None:
    account = await register(client)
    activity, item = await tied_item(client, account)
    await change_item(client, account, item["id"], title="Mine, edited")
    kept = await read_item(client, account, item)
    answer = (await change(client, account, activity["id"], action="skip", apply=True)).json()
    assert [row["reason"] for row in answer["kept"]] == ["edited"]

    assert (await undo(client, account, answer["event_id"])).status_code == 200

    assert await read_item(client, account, item) == kept
    assert (await read_activity(client, account, activity["id"]))["status"] == "planned"


async def test_an_activitys_making_is_not_undone_while_items_are_tied_to_it(
    client: AsyncClient,
) -> None:
    account = await register(client)
    activity, item = await tied_item(client, account)
    making = next(
        event
        for event in await history(client, account, activity["trip_id"])
        if event["kind"] == "activity_added"
    )

    refused = await undo(client, account, making["id"])

    assert (refused.status_code, refused.json()["code"]) == (409, "TRAVEL_UNDO_STALE")
    assert (await read_activity(client, account, activity["id"]))["title"] == "Beach"

    await client.delete(f"{CHECKLIST}/{item['id']}", headers=account.headers)
    assert (await undo(client, account, making["id"])).status_code == 200


async def test_an_item_is_not_brought_back_for_someone_no_longer_on_the_trip(
    client: AsyncClient,
) -> None:
    account = await register(client)
    trip = await add_trip(client, account, travellers=[{"name": "Asha"}, {"name": "Ravi"}])
    asha, ravi = trip["travellers"]
    removed = await add_item(client, account, trip["id"], traveller_id=asha["id"])
    renamed = await add_item(client, account, trip["id"], traveller_id=asha["id"])
    await client.delete(f"{CHECKLIST}/{removed['id']}", headers=account.headers)
    removal = (await history(client, account, trip["id"]))[0]
    await change_item(client, account, renamed["id"], traveller_id=ravi["id"])
    renaming = (await history(client, account, trip["id"]))[0]
    url = f"{TRIPS}/{trip['id']}"
    left = await client.patch(
        url, headers=account.headers, json={"travellers": [ravi], "revision_no": 1}
    )
    assert left.status_code == 200, left.text

    for event in (removal, renaming):
        refused = await undo(client, account, event["id"])
        assert (refused.status_code, refused.json()["code"]) == (409, "TRAVEL_UNDO_STALE")

    edited = await client.patch(url, headers=account.headers, json={"title": "B", "revision_no": 2})
    assert edited.status_code == 200, edited.text  # nobody gone is left named on it


async def test_an_item_is_not_brought_back_off_its_activitys_day(client: AsyncClient) -> None:
    account = await register(client)
    activity, item = await tied_item(client, account)
    await client.delete(f"{CHECKLIST}/{item['id']}", headers=account.headers)
    removal = (await history(client, account, activity["trip_id"]))[0]
    moved = await change(
        client, account, activity["id"], action="reschedule", new_day=LAST_DAY, apply=True
    )
    assert moved.status_code == 200, moved.text

    refused = await undo(client, account, removal["id"])

    assert (refused.status_code, refused.json()["code"]) == (409, "TRAVEL_UNDO_STALE")
    assert refused.json()["details"] == {"record_ids": [item["id"]]}
    assert await items_of(client, account, activity["trip_id"]) == []

    assert (await undo(client, account, moved.json()["event_id"])).status_code == 200
    assert (await undo(client, account, removal["id"])).status_code == 200  # back on its day
    assert (await read_item(client, account, item))["day"] == FIRST_DAY


# ---------------------------------------------------------------------- removing an activity


async def test_removing_an_activity_stands_its_items_down_and_an_undo_brings_all_back(
    client: AsyncClient,
) -> None:
    account = await register(client)
    activity, untouched = await tied_item(client, account, title="Untouched")
    trip_id = activity["trip_id"]
    mine = await add_item(client, account, trip_id, title="Mine", activity_id=activity["id"])
    await change_item(client, account, mine["id"], title="Mine, edited")
    mine_now = await read_item(client, account, mine)
    url = f"{ACTIVITIES}/{activity['id']}"

    removed = await client.delete(url, headers=account.headers)

    assert removed.status_code == 200, removed.text
    assert (await read_item(client, account, untouched))["state"] == "not_applicable"
    assert await read_item(client, account, mine) == mine_now  # kept, and still there to see
    event = (await history(client, account, trip_id))[0]
    assert (event["kind"], event["id"]) == ("activity_deleted", removed.json()["event_id"])
    assert event["summary"]["kept"] == 1
    assert {line["title"] for line in event["changes"]} == {"Beach", "Untouched"}

    assert (await undo(client, account, event["id"])).status_code == 200

    assert (await client.get(url, headers=account.headers)).status_code == 200
    now = await read_item(client, account, untouched)
    assert (now["state"], now["edited"]) == ("not_started", False)


async def test_an_item_kept_after_its_activity_went_can_still_be_changed(
    client: AsyncClient,
) -> None:
    account = await register(client)
    activity, item = await tied_item(client, account)
    await change_item(client, account, item["id"], title="Mine")
    await client.delete(f"{ACTIVITIES}/{activity['id']}", headers=account.headers)

    moved = await change_item(client, account, item["id"], day=SECOND_DAY, quantity=2)
    freed = await change_item(client, account, item["id"], activity_id=None)

    assert (moved.status_code, moved.json()["day"], moved.json()["quantity"]) == (
        200,
        SECOND_DAY,
        2,
    )
    assert (freed.status_code, freed.json()["activity_id"]) == (200, None)
