"""A change or an undo that fails partway, whyever (a refusal, or the database dropping out): what
it wrote is taken back, each step on its own, so one that can't be doesn't stop the rest, and what
someone else wrote since stays theirs. A write that lands and then fails is taken back as well.
Whatever couldn't be taken back is in the history all the same, and an undo that failed can be
tried again. What follows a write (a trip's reminder) runs once the change is recorded: failing,
it leaves the change saved and in the history."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from pymongo.errors import AutoReconnect

from app.modules.travel import events
from app.modules.travel.events import ActivityStore, ChecklistStore
from app.modules.travel.models import ChecklistRepository, Step
from app.platform import ActorContext, VersionConflictError
from tests.helpers import Account, register
from tests.modules.travel.conftest import (
    SECOND_DAY,
    TRIPS,
    add_activity,
    add_item,
    add_trip,
    change,
    history,
    read_activity,
    read_item,
    undo,
)

WRITE, REVERT, ITEM_REVERT = ChecklistStore.write, ActivityStore.revert, ChecklistStore.revert


async def skip_with_two(client: AsyncClient, account: Account) -> dict[str, Any]:
    """A trip whose activity has two items of its own, A and B, in that order."""
    trip = await add_trip(client, account)
    activity = await add_activity(client, account, trip["id"])
    tied = {"activity_id": activity["id"]}
    first = await add_item(client, account, trip["id"], title="A", **tied)
    second = await add_item(client, account, trip["id"], title="B", **tied)
    return {"trip": trip, "activity": activity, "a": first, "b": second}


def failing_on(record_id: str, error: Exception, *, first: Any = None) -> Any:
    """``ChecklistStore.write`` failing on one item before it writes, after ``first`` (a write
    made elsewhere)."""

    async def write(store: ChecklistStore, step: Step, back: Step, source: str) -> Step:
        if step.record_id == record_id:
            if first:
                await first(store)
            raise error
        return await WRITE(store, step, back, source)

    return write


def reverting_but(record_id: str) -> Any:
    """``ChecklistStore.revert`` failing on one item alone."""

    async def revert(store: ChecklistStore, step: Step, back: Step) -> int | None:
        if back.record_id == record_id:
            raise AutoReconnect("gone again")
        return await ITEM_REVERT(store, step, back)

    return revert


async def states(client: AsyncClient, account: Account, plan: dict[str, Any]) -> tuple[str, ...]:
    activity = await read_activity(client, account, plan["activity"]["id"])
    a = await read_item(client, account, plan["a"])
    b = await read_item(client, account, plan["b"])
    return activity["status"], a["state"], b["state"]


async def test_one_take_back_refused_does_not_stop_the_rest(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B is refused just after A was ticked on another phone. A's tick stays (it is theirs), and
    the activity, before it, still goes back."""
    account = await register(client)
    plan = await skip_with_two(client, account)
    events_before = len(await history(client, account, plan["trip"]["id"]))

    async def ticked_elsewhere(store: ChecklistStore) -> None:
        await store.repository.update(plan["a"]["id"], {"state": "completed"})

    monkeypatch.setattr(
        ChecklistStore,
        "write",
        failing_on(plan["b"]["id"], VersionConflictError(), first=ticked_elsewhere),
    )
    response = await change(client, account, plan["activity"]["id"], action="skip", apply=True)

    assert (response.status_code, response.json()["code"]) == (409, "VERSION_CONFLICT")
    assert await states(client, account, plan) == ("planned", "completed", "not_started")
    assert len(await history(client, account, plan["trip"]["id"])) == events_before


async def test_a_failure_that_is_not_a_refusal_is_taken_back_too(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = await register(client)
    plan = await skip_with_two(client, account)
    events_before = len(await history(client, account, plan["trip"]["id"]))
    monkeypatch.setattr(ChecklistStore, "write", failing_on(plan["b"]["id"], AutoReconnect("gone")))

    response = await change(client, account, plan["activity"]["id"], action="skip", apply=True)

    assert response.status_code == 500
    assert await states(client, account, plan) == ("planned", "not_started", "not_started")
    assert len(await history(client, account, plan["trip"]["id"])) == events_before


async def test_a_write_refused_leaves_what_someone_else_wrote_meanwhile(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another phone sets B to not applicable just before this change writes it. The change is
    refused there, and B keeps the other phone's write, though it is what this one would have
    written: a refused write wrote nothing, so there is nothing of it to take back."""
    account = await register(client)
    plan = await skip_with_two(client, account)

    async def written_elsewhere_first(
        store: ChecklistStore, step: Step, back: Step, source: str
    ) -> Step:
        if step.record_id == plan["b"]["id"]:
            await store.repository.update(plan["b"]["id"], {"state": "not_applicable"})
        return await WRITE(store, step, back, source)

    monkeypatch.setattr(ChecklistStore, "write", written_elsewhere_first)

    response = await change(client, account, plan["activity"]["id"], action="skip", apply=True)

    assert (response.status_code, response.json()["code"]) == (409, "VERSION_CONFLICT")
    assert await states(client, account, plan) == ("planned", "not_started", "not_applicable")


async def test_a_write_that_lands_and_then_fails_is_taken_back_too(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B's write is in, and the read after it fails: B is put back with the rest, as if it
    had never been written, and nothing is said of a change that isn't."""
    account = await register(client)
    plan = await skip_with_two(client, account)
    events_before = len(await history(client, account, plan["trip"]["id"]))
    update = ChecklistRepository.update

    async def lands_then_fails(
        repository: ChecklistRepository, entity_id: str, changes: Any, **options: Any
    ) -> Any:
        written = await update(repository, entity_id, changes, **options)
        if str(entity_id) == plan["b"]["id"] and options.get("source") != "undo":
            raise AutoReconnect("gone after the write")
        return written

    monkeypatch.setattr(ChecklistRepository, "update", lands_then_fails)

    response = await change(client, account, plan["activity"]["id"], action="skip", apply=True)

    assert response.status_code == 500
    assert await states(client, account, plan) == ("planned", "not_started", "not_started")
    assert len(await history(client, account, plan["trip"]["id"])) == events_before


async def test_what_could_not_be_taken_back_is_in_the_history_and_undone_from_it(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = await register(client)
    plan = await skip_with_two(client, account)
    monkeypatch.setattr(ChecklistStore, "write", failing_on(plan["b"]["id"], AutoReconnect("gone")))

    async def lost(_store: ActivityStore, _step: Step, _back: Step) -> None:
        raise AutoReconnect("gone again")

    monkeypatch.setattr(ActivityStore, "revert", lost)

    response = await change(client, account, plan["activity"]["id"], action="skip", apply=True)

    assert response.status_code == 500
    assert await states(client, account, plan) == ("skipped", "not_started", "not_started")
    stayed = (await history(client, account, plan["trip"]["id"]))[0]
    assert (stayed["kind"], stayed["undoable"]) == ("activity_skipped", True)
    assert [line["record_id"] for line in stayed["changes"]] == [plan["activity"]["id"]]

    monkeypatch.setattr(ChecklistStore, "write", WRITE)
    monkeypatch.setattr(ActivityStore, "revert", REVERT)
    assert (await undo(client, account, stayed["id"])).status_code == 200
    assert (await read_activity(client, account, plan["activity"]["id"]))["status"] == "planned"


async def test_a_take_back_that_fails_midway_still_takes_back_those_before_it(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B fails; taking A back fails too. The activity, written before A, still goes back, and
    only A, left written, is in the history, to be undone from there."""
    account = await register(client)
    plan = await skip_with_two(client, account)
    monkeypatch.setattr(ChecklistStore, "write", failing_on(plan["b"]["id"], AutoReconnect("gone")))
    monkeypatch.setattr(ChecklistStore, "revert", reverting_but(plan["a"]["id"]))

    response = await change(client, account, plan["activity"]["id"], action="skip", apply=True)

    assert response.status_code == 500
    assert await states(client, account, plan) == ("planned", "not_applicable", "not_started")
    stayed = (await history(client, account, plan["trip"]["id"]))[0]
    assert (stayed["kind"], stayed["undoable"]) == ("activity_skipped", True)
    assert [line["record_id"] for line in stayed["changes"]] == [plan["a"]["id"]]

    monkeypatch.setattr(ChecklistStore, "write", WRITE)
    monkeypatch.setattr(ChecklistStore, "revert", ITEM_REVERT)
    assert (await undo(client, account, stayed["id"])).status_code == 200
    assert await states(client, account, plan) == ("planned", "not_started", "not_started")


async def test_an_undo_that_fails_partway_is_put_back_and_can_be_tried_again(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its last step (the activity's) fails once the items' are done: they go back to as the
    change left them, and nothing says it was undone."""
    account = await register(client)
    plan = await skip_with_two(client, account)
    skipped = await change(client, account, plan["activity"]["id"], action="skip", apply=True)
    event_id = skipped.json()["event_id"]
    activity_write = ActivityStore.write

    async def dropped(store: ActivityStore, step: Step, back: Step, source: str) -> Step:
        if source == "undo":
            raise AutoReconnect("gone")
        return await activity_write(store, step, back, source)

    monkeypatch.setattr(ActivityStore, "write", dropped)

    assert (await undo(client, account, event_id)).status_code == 500
    assert await states(client, account, plan) == ("skipped", "not_applicable", "not_applicable")
    event = next(
        item
        for item in await history(client, account, plan["trip"]["id"])
        if item["id"] == event_id
    )
    assert (event["undoable"], event["undone_at"]) == (True, None)

    monkeypatch.setattr(ActivityStore, "write", activity_write)
    assert (await undo(client, account, event_id)).status_code == 200
    assert await states(client, account, plan) == ("planned", "not_started", "not_started")


async def test_an_undo_that_stays_partly_done_says_how_far_and_is_stale(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = await register(client)
    plan = await skip_with_two(client, account)
    skipped = await change(client, account, plan["activity"]["id"], action="skip", apply=True)
    event_id = skipped.json()["event_id"]
    activity_write = ActivityStore.write

    async def dropped(store: ActivityStore, step: Step, back: Step, source: str) -> Step:
        if source == "undo":
            raise AutoReconnect("gone")
        return await activity_write(store, step, back, source)

    monkeypatch.setattr(ActivityStore, "write", dropped)
    monkeypatch.setattr(ChecklistStore, "revert", reverting_but(plan["b"]["id"]))

    assert (await undo(client, account, event_id)).status_code == 500
    assert await states(client, account, plan) == ("skipped", "not_applicable", "not_started")
    newest = (await history(client, account, plan["trip"]["id"]))[0]
    assert (newest["kind"], newest["summary"]["event_id"]) == ("undo", event_id)
    assert [line["record_id"] for line in newest["changes"]] == [plan["b"]["id"]]

    monkeypatch.setattr(ActivityStore, "write", activity_write)
    refused = await undo(client, account, event_id)
    assert (refused.status_code, refused.json()["code"]) == (409, "TRAVEL_UNDO_STALE")


# ---------------------------------------------------------------------- what follows a write


async def no_reminder(_ctx: ActorContext, _trip: Any) -> None:
    raise AutoReconnect("gone while reminding")


async def trip_now(client: AsyncClient, account: Account, trip_id: str) -> dict[str, Any]:
    return dict((await client.get(f"{TRIPS}/{trip_id}", headers=account.headers)).json())


async def test_a_reminder_that_fails_leaves_the_trip_changed_and_its_change_undoable(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dates are saved, then their reminder can't be moved: the answer says it failed, but the
    change is in the history, and undoing it puts the dates and the reminder back."""
    account = await register(client)
    trip = await add_trip(client, account, remind_prepare=True)
    monkeypatch.setattr(events, "sync_reminder", no_reminder)

    response = await client.patch(
        f"{TRIPS}/{trip['id']}",
        headers=account.headers,
        json={"start_date": SECOND_DAY, "revision_no": 1},
    )

    assert response.status_code == 500
    assert (await trip_now(client, account, trip["id"]))["start_date"] == SECOND_DAY
    edited = (await history(client, account, trip["id"]))[0]
    assert (edited["kind"], edited["undoable"]) == ("trip_edited", True)

    monkeypatch.undo()
    assert (await undo(client, account, edited["id"])).status_code == 200
    assert (await trip_now(client, account, trip["id"]))["start_date"] == trip["start_date"]
    page = (await client.get("/v1/reminders", headers=account.headers)).json()
    assert [item["due_on"] for item in page["items"]] == [trip["start_date"]]


async def test_a_reminder_that_fails_after_an_undo_leaves_it_undone(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trip is put back, then its reminder can't be: the undo is recorded and the edit is
    undone, so trying again says so instead of calling it stale."""
    account = await register(client)
    trip = await add_trip(client, account, remind_prepare=True)
    moved = await client.patch(
        f"{TRIPS}/{trip['id']}",
        headers=account.headers,
        json={"start_date": SECOND_DAY, "revision_no": 1},
    )
    edit_id = moved.json()["event_id"]
    monkeypatch.setattr(events, "sync_reminder", no_reminder)

    assert (await undo(client, account, edit_id)).status_code == 500

    assert (await trip_now(client, account, trip["id"]))["start_date"] == trip["start_date"]
    newest, edited, *_ = await history(client, account, trip["id"])
    assert (newest["kind"], newest["summary"]["event_id"]) == ("undo", edit_id)
    assert (edited["id"], edited["undoable"]) == (edit_id, False)
    assert edited["undone_at"] is not None
    again = await undo(client, account, edit_id)
    assert (again.status_code, again.json()["code"]) == (409, "TRAVEL_UNDO_DONE")
