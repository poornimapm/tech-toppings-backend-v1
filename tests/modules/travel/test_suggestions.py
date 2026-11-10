"""Suggestions by rule (data/travel.yaml): stored as suggestions, made from the trip's real
activities and its length, a preview until applied, and never over the person's word: asking again
adds only what isn't there, and a suggestion removed stays removed (behaviour rule 4)."""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Callable
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import ValidationError

from app.modules.travel.rules import DATA, Rules, SuggestedItem, rules
from tests.helpers import actor_of, register
from tests.modules.travel.conftest import (
    CHECKLIST,
    FIRST_DAY,
    SECOND_DAY,
    add_activity,
    add_item,
    add_trip,
    change,
    change_item,
    history,
    items_of,
    stored,
    suggest,
    undo,
)


def keys(kind: str, *, packing: bool = False) -> set[str]:
    """What the rules suggest for an activity of this kind: only what goes in a bag, to pack."""
    found = rules().suggestions.activities.get(kind, ())
    return {rule.key for rule in found if rule.bag or not packing}


def rule_keys(items: list[dict[str, Any]]) -> set[str]:
    """The rules these items came from: a suggestion's key ends with its rule's."""
    return {item["suggestion_key"].rsplit(":", 1)[-1] for item in items}


def shown_keys(answer: dict[str, Any]) -> list[str]:
    """The suggestion keys a preview or a change shows, in order."""
    return [
        next(line["new"] for line in change["fields"] if line["field"] == "suggestion_key")
        for change in answer["what_changed"]
    ]


# ---------------------------------------------------------------------- preview and apply


async def test_a_preview_writes_nothing_and_applying_adds_what_it_showed(
    client: AsyncClient,
) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    beach = await add_activity(client, account, trip["id"])
    before = await stored(account)

    preview = await suggest(client, account, trip["id"])
    assert preview.status_code == 200, preview.text
    assert await stored(account) == before
    shown = preview.json()
    assert (shown["event_id"], shown["more"]) == (None, False)
    assert {line["record_id"] for line in shown["what_changed"]} == {None}

    applied = (await suggest(client, account, trip["id"], apply=True)).json()

    items = await items_of(client, account, trip["id"])
    assert rule_keys(items) == keys("beach")
    for item in items:
        assert (item["source"], item["edited"], item["state"]) == ("rule", False, "not_started")
        assert (item["activity_id"], item["day"]) == (beach["id"], FIRST_DAY)
        assert item["suggestion_key"].startswith(f"activity:{beach['id']}:")
    assert shown_keys(applied) == shown_keys(shown)
    assert {line["record_id"] for line in applied["what_changed"]} == {item["id"] for item in items}
    assert applied["days"] == [FIRST_DAY]
    event = (await history(client, account, trip["id"]))[0]
    assert (event["id"], event["kind"], event["undoable"]) == (
        applied["event_id"],
        "checklist_suggested",
        True,
    )
    assert event["summary"] == {"count": len(items), "packing": False, "day": None}


async def test_a_trip_with_nothing_to_suggest_from_changes_nothing(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    before = await stored(account)

    answer = await suggest(client, account, trip["id"], apply=True)

    assert answer.status_code == 200, answer.text
    assert (answer.json()["what_changed"], answer.json()["event_id"]) == ([], None)
    assert await stored(account) == before  # no item, and no event either


async def test_suggestions_come_only_from_activities_still_to_come(
    app: FastAPI, client: AsyncClient
) -> None:
    account = await register(client)
    today = (await actor_of(app, account)).today()

    def day(offset: int) -> str:
        return (today + dt.timedelta(days=offset)).isoformat()

    trip = await add_trip(client, account, start_date=day(-2), end_date=day(2))
    plain = next(kind for kind in rules().activities.kinds if not keys(kind))
    await add_activity(
        client, account, trip["id"], title="Nothing to bring", kind=plain, day=day(0)
    )
    await add_activity(client, account, trip["id"], title="Gone by", day=day(-1))
    skipped = await add_activity(client, account, trip["id"], title="Skipped", day=day(0))
    assert (await change(client, account, skipped["id"], action="skip", apply=True)).is_success
    hike = await add_activity(client, account, trip["id"], kind="hike", day=day(1))

    await suggest(client, account, trip["id"], apply=True)

    items = await items_of(client, account, trip["id"])
    assert rule_keys(items) == keys("hike")
    assert {item["activity_id"] for item in items} == {hike["id"]}


async def test_a_day_suggests_only_for_that_days_activities(client: AsyncClient) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    await add_activity(client, account, trip["id"])
    hike = await add_activity(client, account, trip["id"], kind="hike", day=SECOND_DAY)
    checklist = (await suggest(client, account, trip["id"], day=SECOND_DAY)).json()
    packing = (await suggest(client, account, trip["id"], day=SECOND_DAY, packing=True)).json()
    outside = await suggest(client, account, trip["id"], day="2030-03-09")

    tied = f"activity:{hike['id']}:"
    assert {key.removeprefix(tied) for key in shown_keys(checklist)} == keys("hike")
    assert {key.removeprefix(tied) for key in shown_keys(packing)} == keys("hike", packing=True)
    assert {line["day"] for line in [*checklist["what_changed"], *packing["what_changed"]]} == {
        SECOND_DAY
    }  # the list for the whole trip belongs to no one day
    assert (outside.status_code, outside.json()["code"]) == (422, "TRAVEL_DAY_OUTSIDE_TRIP")


# ---------------------------------------------------------------------- packing by bag and length


def test_a_quantity_is_one_for_every_so_many_days_up_to_a_limit() -> None:
    scaled = SuggestedItem(key="bottoms", title="Bottoms", per_days=2, up_to=4)
    once = SuggestedItem(key="charger", title="Charger")

    assert [scaled.quantity_for(days) for days in (1, 2, 3, 5, 8, 30)] == [1, 1, 2, 3, 4, 4]
    assert once.quantity_for(30) == 1


async def test_the_packing_list_is_by_bag_with_quantities_by_the_trips_length(
    client: AsyncClient,
) -> None:
    account = await register(client)
    packing = rules().suggestions.packing
    longest = max(rule.per_days * rule.up_to for rule in packing if rule.per_days and rule.up_to)
    start = dt.date.fromisoformat(FIRST_DAY)
    short = await add_trip(client, account, end_date=FIRST_DAY)
    long = await add_trip(
        client, account, end_date=(start + dt.timedelta(days=longest - 1)).isoformat()
    )
    hike = await add_activity(client, account, long["id"], kind="hike")

    for trip in (short, long):
        answer = await suggest(client, account, trip["id"], packing=True, apply=True)
        assert answer.status_code == 200, answer.text

    async def whole(trip: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """The list for the whole trip, by suggestion key."""
        found = await items_of(client, account, trip["id"])
        return {item["suggestion_key"]: item for item in found if not item["activity_id"]}

    one_day, many_days = await whole(short), await whole(long)
    for rule in packing:
        for found in (one_day, many_days):
            item = found[f"packing:{rule.key}"]
            assert (item["title"], item["bag"], item["day"]) == (rule.title, rule.bag, None)
        assert one_day[f"packing:{rule.key}"]["quantity"] == 1
        assert many_days[f"packing:{rule.key}"]["quantity"] == (rule.up_to or 1)
    tied = [item for item in await items_of(client, account, long["id"]) if item["activity_id"]]
    assert rule_keys(tied) == keys("hike", packing=True)
    assert {item["activity_id"] for item in tied} == {hike["id"]}


# ---------------------------------------------------------------------- the person's word wins


async def test_asking_again_adds_only_what_is_missing_and_leaves_the_persons_work(
    client: AsyncClient,
) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    await add_activity(client, account, trip["id"])
    await suggest(client, account, trip["id"], packing=True, apply=True)
    edited, done, removed, *_ = await items_of(client, account, trip["id"])
    await change_item(client, account, edited["id"], title="My own", quantity=3)
    await change_item(client, account, done["id"], state="completed")
    await client.delete(f"{CHECKLIST}/{removed['id']}", headers=account.headers)
    held = await stored(account)

    again = await suggest(client, account, trip["id"], packing=True, apply=True)

    assert again.status_code == 200, again.text
    assert (again.json()["what_changed"], again.json()["event_id"]) == ([], None)
    assert await stored(account) == held  # every item and its history as it was, no event

    hike = await add_activity(client, account, trip["id"], kind="hike", day=SECOND_DAY)
    more = (await suggest(client, account, trip["id"], packing=True, apply=True)).json()

    assert {key.rsplit(":", 1)[0] for key in shown_keys(more)} == {f"activity:{hike['id']}"}
    assert (await stored(account))["travel_checklist"][: len(held["travel_checklist"])] == held[
        "travel_checklist"
    ]


async def test_a_suggestion_ticked_is_still_one_and_renamed_is_the_persons_own(
    client: AsyncClient,
) -> None:
    """A tick or a new quantity makes it the person's (``edited``: rules leave it be), but it is
    still the suggestion as worded, so the app keeps its own words for it and calls it one."""
    account = await register(client)
    trip = await add_trip(client, account)
    await suggest(client, account, trip["id"], packing=True, apply=True)
    tops, bottoms, *_ = await items_of(client, account, trip["id"])
    assert (tops["as_suggested"], tops["edited"]) == (True, False)

    await change_item(client, account, tops["id"], state="completed")
    await change_item(client, account, tops["id"], quantity=2)
    await change_item(client, account, bottoms["id"], title="Shorts")

    after = {item["id"]: item for item in await items_of(client, account, trip["id"])}
    assert (after[tops["id"]]["as_suggested"], after[tops["id"]]["edited"]) == (True, True)
    assert (after[bottoms["id"]]["as_suggested"], after[bottoms["id"]]["edited"]) == (False, True)


async def test_the_history_names_a_suggestion_by_its_key_while_it_is_one(
    client: AsyncClient,
) -> None:
    """So the app words a line of an edit, a removal or a change of its activity as it words the
    item, in the reader's language; a renamed one is told in the person's words."""
    account = await register(client)
    trip = await add_trip(client, account)
    beach = await add_activity(client, account, trip["id"])
    await suggest(client, account, trip["id"], apply=True)
    first, second, *_ = await items_of(client, account, trip["id"])
    await change_item(client, account, second["id"], title="My towel")

    skipped = (await change(client, account, beach["id"], action="skip", apply=True)).json()
    lines = {line["record_id"]: line for line in skipped["what_changed"]}
    assert lines[first["id"]]["suggestion_key"] == first["suggestion_key"]
    assert second["id"] not in lines  # renamed, so the person's own work: kept as it is

    await client.delete(f"{CHECKLIST}/{first['id']}", headers=account.headers)
    await client.delete(f"{CHECKLIST}/{second['id']}", headers=account.headers)
    newest, before = (await history(client, account, trip["id"]))[:2]
    assert newest["changes"][0]["suggestion_key"] is None  # renamed: the person's own words
    assert newest["summary"]["suggestion_key"] is None
    assert before["changes"][0]["suggestion_key"] == first["suggestion_key"]
    assert before["summary"]["suggestion_key"] == first["suggestion_key"]  # its headline too


async def test_what_a_change_keeps_names_a_suggestion_by_its_key_while_it_is_one(
    client: AsyncClient,
) -> None:
    """A ticked suggestion is the person's work, kept when its activity goes: listed in the app's
    own words for it, in the preview and the change alike. A renamed one is in the person's."""
    account = await register(client)
    trip = await add_trip(client, account)
    beach = await add_activity(client, account, trip["id"])
    await suggest(client, account, trip["id"], apply=True)
    ticked, renamed, *_ = await items_of(client, account, trip["id"])
    await change_item(client, account, ticked["id"], state="completed")
    await change_item(client, account, renamed["id"], title="My towel")

    for apply in (False, True):
        answer = (await change(client, account, beach["id"], action="skip", apply=apply)).json()
        kept = {flag["record_id"]: flag for flag in answer["kept"]}
        assert (kept[ticked["id"]]["reason"], kept[ticked["id"]]["suggestion_key"]) == (
            "completed",
            ticked["suggestion_key"],
        )
        assert (kept[renamed["id"]]["title"], kept[renamed["id"]]["suggestion_key"]) == (
            "My towel",
            None,
        )


async def test_a_removed_suggestion_stays_gone_until_its_removal_is_undone(
    client: AsyncClient,
) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    await suggest(client, account, trip["id"], packing=True, apply=True)
    tops = next(
        item
        for item in await items_of(client, account, trip["id"])
        if item["suggestion_key"] == "packing:tops"
    )
    await client.delete(f"{CHECKLIST}/{tops['id']}", headers=account.headers)
    removal = (await history(client, account, trip["id"]))[0]

    again = (await suggest(client, account, trip["id"], packing=True, apply=True)).json()
    assert again["what_changed"] == []

    assert (await undo(client, account, removal["id"])).status_code == 200
    back = [
        item["id"]
        for item in await items_of(client, account, trip["id"])
        if item["suggestion_key"] == "packing:tops"
    ]
    assert back == [tops["id"]]
    assert (await suggest(client, account, trip["id"], packing=True)).json()["what_changed"] == []


async def test_undone_suggestions_were_never_decided_on_and_can_be_offered_again(
    client: AsyncClient,
) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    first = (await suggest(client, account, trip["id"], packing=True, apply=True)).json()

    undone = await undo(client, account, first["event_id"])

    assert undone.status_code == 200, undone.text
    assert await items_of(client, account, trip["id"]) == []
    again = (await suggest(client, account, trip["id"], packing=True)).json()
    assert shown_keys(again) == shown_keys(first)


async def test_suggestions_are_not_undone_once_one_of_them_has_changed(
    client: AsyncClient,
) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    first = (await suggest(client, account, trip["id"], packing=True, apply=True)).json()
    ticked = (await items_of(client, account, trip["id"]))[0]
    await change_item(client, account, ticked["id"], state="completed")
    before = await stored(account)

    refused = await undo(client, account, first["event_id"])

    assert (refused.status_code, refused.json()["code"]) == (409, "TRAVEL_UNDO_STALE")
    assert refused.json()["details"] == {"record_ids": [ticked["id"]]}
    assert await stored(account) == before


# ---------------------------------------------------------------------- limits


async def test_one_request_adds_at_most_the_rule_and_says_when_more_wait(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    smaller = rules().suggestions.model_copy(update={"max_per_run": 5})
    monkeypatch.setattr(
        "app.modules.travel.suggestions.rules",
        lambda: rules().model_copy(update={"suggestions": smaller}),
    )
    wanted = len(rules().suggestions.packing)

    rounds: list[tuple[int, bool]] = []
    for _ in range(wanted):
        answer = (await suggest(client, account, trip["id"], packing=True, apply=True)).json()
        rounds.append((len(answer["what_changed"]), answer["more"]))
        if not answer["more"]:
            break

    assert all(count <= 5 for count, _ in rounds)
    assert [more for _, more in rounds] == [True] * (len(rounds) - 1) + [False]
    assert sum(count for count, _ in rounds) == wanted
    assert len(await items_of(client, account, trip["id"])) == wanted


async def test_suggestions_stop_where_the_trips_checklist_is_full(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    await add_item(client, account, trip["id"])
    limits = rules().checklist.model_copy(update={"max_per_trip": 3})
    monkeypatch.setattr(
        "app.modules.travel.suggestions.rules",
        lambda: rules().model_copy(update={"checklist": limits}),
    )

    filled = (await suggest(client, account, trip["id"], packing=True, apply=True)).json()
    full = await suggest(client, account, trip["id"], packing=True, apply=True)

    assert (len(filled["what_changed"]), filled["more"]) == (2, True)
    assert (full.status_code, full.json()["code"]) == (409, "TRAVEL_CHECKLIST_FULL")


async def test_requests_at_the_same_moment_never_add_a_suggestion_twice(
    client: AsyncClient,
) -> None:
    account = await register(client)
    trip = await add_trip(client, account)
    await add_activity(client, account, trip["id"])

    answers = await asyncio.gather(
        *(suggest(client, account, trip["id"], packing=True, apply=True) for _ in range(3))
    )

    assert {answer.status_code for answer in answers} <= {200, 409}
    for answer in answers:
        if answer.status_code == 409:
            assert answer.json()["code"] == "TRAVEL_SUGGESTION_TAKEN"
    found = [item["suggestion_key"] for item in await items_of(client, account, trip["id"])]
    assert len(found) == len(set(found))
    assert len(found) == len(rules().suggestions.packing) + len(keys("beach", packing=True))
    kinds = [event["kind"] for event in await history(client, account, trip["id"])]
    assert kinds.count("checklist_suggested") == 1


# ---------------------------------------------------------------------- the rules themselves


def _packing(data: dict[str, Any]) -> list[dict[str, Any]]:
    return list(data["suggestions"]["packing"])


@pytest.mark.parametrize(
    "spoil",
    [
        lambda data: data["suggestions"]["activities"].update({"skydiving": []}),
        lambda data: data["suggestions"]["packing"].append(_packing(data)[0]),
        lambda data: data["suggestions"]["packing"].append(
            {"key": "hats", "title": "Hats", "per_days": 2}
        ),
        lambda data: data["suggestions"]["packing"].append(
            {"key": "hats", "title": "Hats", "bag": "rucksack"}
        ),
        lambda data: data["suggestions"].update({"note": "extra"}),
    ],
    ids=["unknown kind", "a key twice", "per_days alone", "unknown bag", "unknown field"],
)
def test_the_rules_are_strict(spoil: Callable[[dict[str, Any]], object]) -> None:
    data = yaml.safe_load(DATA.read_text(encoding="utf-8"))
    spoil(data)

    with pytest.raises(ValidationError):
        Rules.model_validate(data)
