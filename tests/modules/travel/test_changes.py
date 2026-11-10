"""The change flow: a preview writes nothing, applying writes only what depends on the activity and
records one event, and an undo reverses that event unless something it touched has changed since
(behaviour rules 3 and 5, as far as activities and events go)."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from app.modules.travel import changes as flow
from app.modules.travel.events import ACTIVITY, edit
from app.modules.travel.impact import Impact
from app.modules.travel.models import ActivityRepository, Trip, TripActivity
from app.modules.travel.schemas import TravelChangeIn, TravelFlag
from app.platform import ActorContext
from tests.helpers import Account, register
from tests.modules.travel.conftest import (
    ACTIVITIES,
    FIRST_DAY,
    LAST_DAY,
    SECOND_DAY,
    TRIPS,
    add_activity,
    add_trip,
    change,
    history,
    read_activity,
    stored,
    timeline,
    undo,
)

CHANGES = [
    {"action": "done", "actual_note": "Lovely"},
    {"action": "skip", "actual_note": "Went shopping instead"},
    {"action": "replace", "new_activity": {"title": "Shopping", "kind": "shopping"}},
    {"action": "reschedule", "new_day": SECOND_DAY, "new_start_time": "16:00"},
]


async def day_with_one(client: AsyncClient, account: Account, **fields: Any) -> dict[str, Any]:
    trip = await add_trip(client, account)
    return await add_activity(client, account, trip["id"], **fields)


@pytest.mark.parametrize("body", CHANGES, ids=lambda body: body["action"])
async def test_a_preview_writes_nothing_at_all(client: AsyncClient, body: dict[str, Any]) -> None:
    account = await register(client)
    activity = await day_with_one(client, account, estimated_cost_minor=5000)
    before = await stored(account)

    response = await change(client, account, activity["id"], **body)

    assert response.status_code == 200, response.text
    assert response.json()["event_id"] is None
    assert response.json()["what_changed"]
    assert await stored(account) == before


@pytest.mark.parametrize("body", CHANGES, ids=lambda body: body["action"])
async def test_what_a_preview_says_is_what_applying_does(
    client: AsyncClient, body: dict[str, Any]
) -> None:
    account = await register(client)
    activity = await day_with_one(client, account, estimated_cost_minor=5000)

    preview = (await change(client, account, activity["id"], **body)).json()
    applied = (await change(client, account, activity["id"], **body, apply=True)).json()

    assert applied["event_id"]

    def shown(answer: dict[str, Any]) -> list[Any]:
        return [
            {**item, "record_id": None if item["op"] == "add" else item["record_id"]}
            for item in answer["what_changed"]
        ]

    assert shown(applied) == shown(preview)
    assert (applied["needs_review"], applied["days"]) == (preview["needs_review"], preview["days"])


async def test_marking_one_done_keeps_what_really_happened(client: AsyncClient) -> None:
    account = await register(client)
    activity = await day_with_one(client, account, estimated_cost_minor=5000)

    answer = (
        await change(
            client, account, activity["id"], action="done", actual_note="Lovely", apply=True
        )
    ).json()

    now = await read_activity(client, account, activity["id"])
    assert (now["status"], now["actual_note"]) == ("done", "Lovely")
    assert answer["needs_review"] == []  # it did happen: its estimate stands
    lines = {item["field"]: item for item in answer["what_changed"][0]["fields"]}
    assert (lines["status"]["old"], lines["status"]["new"]) == ("planned", "done")
    event = (await history(client, account, activity["trip_id"]))[0]
    assert (event["id"], event["kind"]) == (answer["event_id"], "activity_done")
    assert event["summary"]["note"] == "Lovely"


async def test_skipping_lists_its_estimate_for_review_and_never_changes_it(
    client: AsyncClient,
) -> None:
    account = await register(client)
    activity = await day_with_one(client, account, estimated_cost_minor=5000)

    answer = (await change(client, account, activity["id"], action="skip", apply=True)).json()

    now = await read_activity(client, account, activity["id"])
    assert (now["status"], now["estimated_cost_minor"]) == ("skipped", 5000)
    assert answer["needs_review"] == [
        {
            "entity": "activity",
            "record_id": activity["id"],
            "title": "Beach",
            "reason": "estimate",
            "suggestion_key": None,
        }
    ]
    assert answer["kept"] == []
    assert answer["days"] == [FIRST_DAY]


async def test_a_replacement_comes_right_after_what_it_replaced(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    first = await add_activity(client, account, trip["id"], title="First")
    second = await add_activity(client, account, trip["id"], title="Second")
    third = await add_activity(client, account, trip["id"], title="Third")

    answer = (
        await change(
            client,
            account,
            second["id"],
            action="replace",
            actual_note="It rained",
            new_activity={"title": "Museum", "kind": "sightseeing", "start_time": "11:00"},
            apply=True,
        )
    ).json()

    assert await timeline(client, account, trip["id"], FIRST_DAY) == [
        "First",
        "Second",
        "Museum",
        "Third",
    ]
    now = await read_activity(client, account, second["id"])
    assert (now["status"], now["actual_note"]) == ("replaced", "It rained")
    made = next(item for item in answer["what_changed"] if item["op"] == "add")
    museum = await read_activity(client, account, made["record_id"])
    assert (museum["status"], museum["day"], museum["start_time"]) == (
        "planned",
        FIRST_DAY,
        "11:00",
    )
    # Nothing else had to move for it.
    assert await read_activity(client, account, first["id"]) == first
    assert await read_activity(client, account, third["id"]) == third


async def test_rescheduling_moves_it_and_reports_both_days(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    await add_activity(client, account, trip["id"], title="Already there", day=SECOND_DAY)
    activity = await add_activity(client, account, trip["id"], title="Moved", start_time="09:00")

    answer = (
        await change(
            client,
            account,
            activity["id"],
            action="reschedule",
            new_day=SECOND_DAY,
            new_start_time="16:00",
            apply=True,
        )
    ).json()

    assert answer["days"] == [FIRST_DAY, SECOND_DAY]
    now = await read_activity(client, account, activity["id"])
    assert (now["day"], now["start_time"], now["status"]) == (SECOND_DAY, "16:00", "planned")
    assert await timeline(client, account, trip["id"], SECOND_DAY) == ["Already there", "Moved"]
    assert await timeline(client, account, trip["id"], FIRST_DAY) == []


async def test_rescheduling_only_the_time_stays_in_place(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    activity = await add_activity(client, account, trip["id"], title="a", start_time="09:00")
    await add_activity(client, account, trip["id"], title="b")

    answer = (
        await change(
            client, account, activity["id"], action="reschedule", new_start_time="07:00", apply=True
        )
    ).json()

    assert answer["days"] == [FIRST_DAY]
    assert await timeline(client, account, trip["id"], FIRST_DAY) == ["a", "b"]
    assert (await read_activity(client, account, activity["id"]))["start_time"] == "07:00"


async def test_rescheduling_to_where_it_is_changes_nothing(client: AsyncClient) -> None:
    account = await register(client)
    activity = await day_with_one(client, account, start_time="09:00")
    before = await stored(account)

    response = await change(
        client,
        account,
        activity["id"],
        action="reschedule",
        new_day=FIRST_DAY,
        new_start_time="09:00",
        apply=True,
    )

    assert response.status_code == 200, response.text
    assert (response.json()["what_changed"], response.json()["event_id"]) == ([], None)
    assert await stored(account) == before


@pytest.mark.parametrize("apply", [False, True])
async def test_a_reschedule_stays_within_the_trip(client: AsyncClient, apply: bool) -> None:
    account = await register(client)
    activity = await day_with_one(client, account)

    response = await change(
        client, account, activity["id"], action="reschedule", new_day="2030-03-06", apply=apply
    )

    assert (response.status_code, response.json()["code"]) == (422, "TRAVEL_DAY_OUTSIDE_TRIP")
    assert (await read_activity(client, account, activity["id"]))["day"] == FIRST_DAY


@pytest.mark.parametrize(
    "body",
    [
        {"action": "replace"},
        {"action": "reschedule"},
        {"action": "cancel"},
        {"action": "replace", "new_activity": {"title": "x", "kind": "teleport"}},
        {"action": "replace", "new_activity": {"title": "x", "kind": "tour", "day": FIRST_DAY}},
        {"action": "reschedule", "new_start_time": "9am"},
    ],
)
async def test_what_a_change_can_not_be(client: AsyncClient, body: dict[str, Any]) -> None:
    account = await register(client)
    activity = await day_with_one(client, account)

    response = await change(client, account, activity["id"], **body, apply=True)

    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_ERROR")


async def test_a_replacements_booking_must_be_on_the_trip(client: AsyncClient) -> None:
    account = await register(client)
    activity = await day_with_one(client, account)

    response = await change(
        client,
        account,
        activity["id"],
        action="replace",
        new_activity={"title": "Tour", "kind": "tour", "booking_id": "0" * 24},
    )

    assert (response.status_code, response.json()["code"]) == (422, "TRAVEL_BOOKING_UNKNOWN")


@pytest.mark.parametrize("apply", [False, True])
async def test_only_a_planned_activity_changes_this_way(client: AsyncClient, apply: bool) -> None:
    account = await register(client)
    activity = await day_with_one(client, account)
    await change(client, account, activity["id"], action="skip", apply=True)

    response = await change(client, account, activity["id"], action="done", apply=apply)

    assert (response.status_code, response.json()["code"]) == (409, "TRAVEL_ACTIVITY_STATE")
    assert (await read_activity(client, account, activity["id"]))["status"] == "skipped"


async def test_an_apply_after_the_activity_moved_on_is_refused(client: AsyncClient) -> None:
    account = await register(client)
    activity = await day_with_one(client, account)
    await client.patch(
        f"{ACTIVITIES}/{activity['id']}",
        headers=account.headers,
        json={"notes": "Changed since", "revision_no": 1},
    )

    stale = await change(client, account, activity["id"], action="skip", revision_no=1, apply=True)

    assert (stale.status_code, stale.json()["code"]) == (409, "VERSION_CONFLICT")
    assert (await read_activity(client, account, activity["id"]))["status"] == "planned"


@pytest.mark.parametrize("body", CHANGES, ids=lambda body: body["action"])
async def test_another_days_records_never_change(client: AsyncClient, body: dict[str, Any]) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    activity = await add_activity(client, account, trip["id"], title="Changed")
    # Days no change here reaches (a reschedule goes to the second, and one is already there).
    other = await add_activity(
        client, account, trip["id"], title="Other", day="2030-03-03", estimated_cost_minor=9000
    )
    last = await add_activity(client, account, trip["id"], title="Last", day=LAST_DAY)
    moved_to = await add_activity(client, account, trip["id"], title="There", day=SECOND_DAY)
    others = {other["id"], last["id"], moved_to["id"]}

    def of_others(rows: dict[str, list[dict[str, Any]]]) -> list[list[dict[str, Any]]]:
        return [
            [row for row in rows["travel_activities"] if str(row["_id"]) in others],
            [row for row in rows["travel_activity_revisions"] if str(row["entity_id"]) in others],
        ]

    held = of_others(await stored(account))

    response = await change(client, account, activity["id"], **body, apply=True)

    assert response.status_code == 200, response.text
    assert of_others(await stored(account)) == held  # as stored, their histories too
    assert not others & {item["record_id"] for item in response.json()["what_changed"]}


# ---------------------------------------------------------------------- undo (rule 5)


@pytest.mark.parametrize("body", CHANGES, ids=lambda body: body["action"])
async def test_an_undo_puts_everything_back_and_is_itself_an_event(
    client: AsyncClient, body: dict[str, Any]
) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    await add_activity(client, account, trip["id"], title="Already there", day=SECOND_DAY)
    activity = await add_activity(
        client, account, trip["id"], title="Changed", start_time="09:00", estimated_cost_minor=5000
    )
    before_change = await timeline(client, account, trip["id"], FIRST_DAY)
    answer = (await change(client, account, activity["id"], **body, apply=True)).json()

    undone = await undo(client, account, answer["event_id"])

    assert undone.status_code == 200, undone.text
    now = await read_activity(client, account, activity["id"])
    for name in ("day", "start_time", "position", "status", "actual_note"):
        assert now[name] == activity[name], name
    assert await timeline(client, account, trip["id"], FIRST_DAY) == before_change
    assert await timeline(client, account, trip["id"], SECOND_DAY) == ["Already there"]
    event = undone.json()
    assert (event["kind"], event["undoable"]) == ("undo", False)
    assert event["summary"]["event_id"] == answer["event_id"]
    latest, undid, *_ = await history(client, account, trip["id"])
    assert latest["id"] == event["id"]
    assert undid["id"] == answer["event_id"]
    assert undid["undone_at"] is not None
    assert undid["undoable"] is False


async def test_an_undo_says_what_it_put_back(client: AsyncClient) -> None:
    account = await register(client)
    activity = await day_with_one(client, account)
    answer = (await change(client, account, activity["id"], action="skip", apply=True)).json()

    event = (await undo(client, account, answer["event_id"])).json()

    line = event["changes"][0]["fields"][0]
    assert (line["field"], line["old"], line["new"]) == ("status", "skipped", "planned")


async def test_an_undo_is_refused_once_what_it_touched_has_changed(client: AsyncClient) -> None:
    account = await register(client)
    activity = await day_with_one(client, account)
    answer = (await change(client, account, activity["id"], action="skip", apply=True)).json()
    url = f"{ACTIVITIES}/{activity['id']}"
    edited = await client.patch(
        url, headers=account.headers, json={"notes": "Changed since", "revision_no": 2}
    )
    assert edited.status_code == 200, edited.text
    before = await stored(account)

    refused = await undo(client, account, answer["event_id"])

    assert (refused.status_code, refused.json()["code"]) == (409, "TRAVEL_UNDO_STALE")
    assert refused.json()["details"] == {"record_ids": [activity["id"]]}
    assert await stored(account) == before  # nothing written, the event left as it was
    assert (await read_activity(client, account, activity["id"]))["status"] == "skipped"


async def test_an_undo_is_refused_once_a_replacement_has_changed(client: AsyncClient) -> None:
    account = await register(client)
    activity = await day_with_one(client, account)
    answer = (
        await change(
            client,
            account,
            activity["id"],
            action="replace",
            new_activity={"title": "Museum", "kind": "sightseeing"},
            apply=True,
        )
    ).json()
    made = next(item for item in answer["what_changed"] if item["op"] == "add")
    await client.patch(
        f"{ACTIVITIES}/{made['record_id']}",
        headers=account.headers,
        json={"notes": "Booked it", "revision_no": 1},
    )

    refused = await undo(client, account, answer["event_id"])

    assert (refused.status_code, refused.json()["code"]) == (409, "TRAVEL_UNDO_STALE")
    assert refused.json()["details"] == {"record_ids": [made["record_id"]]}
    assert (await read_activity(client, account, activity["id"]))["status"] == "replaced"
    assert (await read_activity(client, account, made["record_id"]))["notes"] == "Booked it"


async def test_an_event_is_undone_once(client: AsyncClient) -> None:
    account = await register(client)
    activity = await day_with_one(client, account)
    answer = (await change(client, account, activity["id"], action="skip", apply=True)).json()
    assert (await undo(client, account, answer["event_id"])).status_code == 200

    again = await undo(client, account, answer["event_id"])

    assert (again.status_code, again.json()["code"]) == (409, "TRAVEL_UNDO_DONE")


async def test_an_undo_and_a_trips_making_can_not_be_undone(client: AsyncClient) -> None:
    account = await register(client)
    activity = await day_with_one(client, account)
    answer = (await change(client, account, activity["id"], action="skip", apply=True)).json()
    the_undo = (await undo(client, account, answer["event_id"])).json()
    making = (await history(client, account, activity["trip_id"]))[-1]
    assert making["kind"] == "trip_created"

    for event_id in (the_undo["id"], making["id"]):
        refused = await undo(client, account, event_id)
        assert (refused.status_code, refused.json()["code"]) == (409, "TRAVEL_UNDO_UNAVAILABLE")


async def test_an_undo_that_no_longer_fits_the_trip_is_refused_and_can_be_tried_again(
    client: AsyncClient,
) -> None:
    """Moved to an early day, then the trip was shortened: its old day is gone."""
    account = await register(client)
    trip = await add_trip(client, account)
    activity = await add_activity(client, account, trip["id"], day=LAST_DAY)
    answer = (
        await change(
            client, account, activity["id"], action="reschedule", new_day=FIRST_DAY, apply=True
        )
    ).json()
    url = f"{TRIPS}/{trip['id']}"
    short = await client.patch(
        url, headers=account.headers, json={"end_date": SECOND_DAY, "revision_no": 1}
    )
    assert short.status_code == 200, short.text

    refused = await undo(client, account, answer["event_id"])

    assert (refused.status_code, refused.json()["code"]) == (422, "TRAVEL_DAY_OUTSIDE_TRIP")
    assert (await read_activity(client, account, activity["id"]))["day"] == FIRST_DAY
    event = next(
        item
        for item in await history(client, account, trip["id"])
        if item["id"] == answer["event_id"]
    )
    assert (event["undoable"], event["undone_at"]) == (True, None)  # not spent by the refusal

    back = await client.patch(
        url, headers=account.headers, json={"end_date": LAST_DAY, "revision_no": 2}
    )
    assert back.status_code == 200, back.text
    assert (await undo(client, account, answer["event_id"])).status_code == 200
    assert (await read_activity(client, account, activity["id"]))["day"] == LAST_DAY


async def test_a_trip_edit_that_no_longer_fits_its_activities_is_not_undone(
    client: AsyncClient,
) -> None:
    account = await register(client)
    trip = await add_trip(client, account, end_date=SECOND_DAY)
    url = f"{TRIPS}/{trip['id']}"
    await client.patch(url, headers=account.headers, json={"end_date": LAST_DAY, "revision_no": 1})
    longer = (await history(client, account, trip["id"]))[0]
    await add_activity(client, account, trip["id"], day=LAST_DAY)

    refused = await undo(client, account, longer["id"])

    assert (refused.status_code, refused.json()["code"]) == (
        409,
        "TRAVEL_ACTIVITIES_OUTSIDE_DATES",
    )
    assert (await client.get(url, headers=account.headers)).json()["end_date"] == LAST_DAY


async def test_a_deleted_trips_events_are_out_of_reach(client: AsyncClient) -> None:
    account = await register(client)
    activity = await day_with_one(client, account)
    answer = (await change(client, account, activity["id"], action="skip", apply=True)).json()
    await client.delete(f"{TRIPS}/{activity['trip_id']}", headers=account.headers)

    refused = await undo(client, account, answer["event_id"])

    assert refused.status_code == 404
    assert (
        await client.get(f"{TRIPS}/{activity['trip_id']}/events", headers=account.headers)
    ).status_code == 404


# ---------------------------------------------------------------------- what later kinds plug into


async def test_another_effect_joins_the_preview_the_change_and_the_undo(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kind of record joins by adding one effect: this one reaches a sibling activity, flags
    one and keeps one. Nothing else in the flow has to know."""
    account = await register(client)
    trip = await add_trip(client, account)
    activity = await add_activity(client, account, trip["id"], title="Changed")
    sibling = await add_activity(client, account, trip["id"], title="Sibling")

    async def sibling_effect(
        ctx: ActorContext, _trip: Trip, _activity: TripActivity, _body: TravelChangeIn
    ) -> Impact:
        record = await ActivityRepository(ctx.scope, ctx.clock).get(sibling["id"])
        flag = TravelFlag(
            entity=ACTIVITY, record_id=sibling["id"], title="Sibling", reason="edited"
        )
        return Impact(
            planned=[edit(ACTIVITY, record, {"notes": "Reached"}, title="Sibling", day=FIRST_DAY)],
            needs_review=[flag],
            kept=[flag],
        )

    monkeypatch.setattr(flow, "EFFECTS", (*flow.EFFECTS, sibling_effect))

    preview = (await change(client, account, activity["id"], action="skip")).json()
    assert {item["title"] for item in preview["what_changed"]} == {"Changed", "Sibling"}
    assert (await read_activity(client, account, sibling["id"]))["notes"] is None

    answer = (await change(client, account, activity["id"], action="skip", apply=True)).json()
    assert (await read_activity(client, account, sibling["id"]))["notes"] == "Reached"
    assert [item["reason"] for item in answer["needs_review"]] == ["edited"]
    assert [item["reason"] for item in answer["kept"]] == ["edited"]
    event = (await history(client, account, trip["id"]))[0]
    assert event["summary"]["kept"] == 1

    assert (await undo(client, account, answer["event_id"])).status_code == 200
    assert (await read_activity(client, account, sibling["id"]))["notes"] is None
    assert (await read_activity(client, account, activity["id"]))["status"] == "planned"


async def test_a_step_refused_partway_takes_back_those_before_it(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    activity = await add_activity(client, account, trip["id"], title="Changed")
    sibling = await add_activity(client, account, trip["id"], title="Sibling")

    async def stale_effect(
        ctx: ActorContext, _trip: Trip, _activity: TripActivity, _body: TravelChangeIn
    ) -> Impact:
        record = await ActivityRepository(ctx.scope, ctx.clock).get(sibling["id"])
        record.revision_no = 99  # as if it moved on from what was read
        return Impact(planned=[edit(ACTIVITY, record, {"notes": "Reached"}, title="Sibling")])

    monkeypatch.setattr(flow, "EFFECTS", (*flow.EFFECTS, stale_effect))
    events_before = len(await history(client, account, trip["id"]))

    response = await change(client, account, activity["id"], action="skip", apply=True)

    assert (response.status_code, response.json()["code"]) == (409, "VERSION_CONFLICT")
    assert (await read_activity(client, account, activity["id"]))["status"] == "planned"
    assert (await read_activity(client, account, sibling["id"]))["notes"] is None
    assert len(await history(client, account, trip["id"])) == events_before  # no event for it
