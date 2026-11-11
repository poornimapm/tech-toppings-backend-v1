"""Basic AI on a trip (Phase 19), with the fake provider: AI runs only on a Basic trip, every
request is checked before a run starts, each task drafts and nothing more, and each draft is
previewed and applied through Travel's own routes, recorded as AI's and undone like any change."""

from __future__ import annotations

import json
from typing import Any

import pytest
import respx
from beanie import PydanticObjectId
from fastapi import FastAPI
from httpx import AsyncClient, Response

from app.modules.travel import trips
from app.modules.travel.rules import rules
from app.platform import to_minor
from app.platform.agents.models import AgentRun
from tests.helpers import Account, register
from tests.modules.travel.conftest import (
    FIRST_DAY,
    LAST_DAY,
    SECOND_DAY,
    TRAVEL,
    TRIPS,
    add_activity,
    add_item,
    add_trip,
    assist,
    change,
    finished,
    history,
    items_of,
    stored,
    suggest,
    timeline,
    undo,
)
from tests.platform.agents.conftest import (
    LLM_URL,
    PRIMARY,
    AgentClient,
    calls,
    give_consent,
    reply,
    sent,
)

AI: dict[str, Any] = {"providers": [PRIMARY]}
HIKE_RULES = rules().suggestions.activities["hike"]
SKILLS = {"describe-trip", "plan-day", "activity-checklist", "update-trip", "ask-trip"}
# What every travel skill tells the model never to make up.
NEVER_INVENTED = ("bookings", "schedules", "opening hours", "prices", "weather", "entry")


async def runs_of(account: Account) -> int:
    return await AgentRun.find({"user_id": PydanticObjectId(account.user_id)}).count()


async def post(client: AsyncClient, account: Account, url: str, **body: Any) -> Response:
    return await client.post(url, headers=account.headers, json=body)


def tool_results(route: respx.Route, index: int) -> list[Any]:
    """What the tools answered, as the model read it in that call."""
    return [json.loads(m["content"]) for m in sent(route, index)["messages"] if m["role"] == "tool"]


def as_applied(activities: list[dict[str, Any]], currency: str) -> list[dict[str, Any]]:
    """A day draft's activities as its page applies them: the AI's cost (main units) in minor
    units of the trip's currency."""
    applied = []
    for activity in activities:
        fields = {name: value for name, value in activity.items() if name != "estimated_cost"}
        cost = activity["estimated_cost"]
        minor = None if cost is None else to_minor(cost, currency)
        applied.append(fields | {"estimated_cost_minor": minor})
    return applied


# ---------------------------------------------------------------------- the agent and the gate


async def test_travel_skills_run_only_from_a_trips_pages(app: FastAPI, client: AsyncClient) -> None:
    account = await register(client)
    agents = (await client.get("/v1/agents", headers=account.headers)).json()["items"]
    from_palette = await post(
        client, account, "/v1/agents/assistant/runs", input="plan my day", skill="plan-day"
    )
    catalog = app.state.agents.catalog

    listed = {agent["name"]: {skill["name"] for skill in agent["skills"]} for agent in agents}
    assert listed["travel"] == SKILLS
    assert not listed["assistant"] & SKILLS  # kept off the general assistant
    assert (from_palette.status_code, from_palette.json()["code"]) == (404, "AGENT_SKILL_NOT_FOUND")
    for name in SKILLS:
        skill = catalog.skills[name]
        assert (skill.agent, skill.owner) == ("travel", "travel")
        assert skill.max_tokens is not None
        assert all(words in skill.instructions for words in NEVER_INVENTED), name
    # Only the activity's help has rules to fall back on; the others say AI is unavailable.
    assert {name for name in SKILLS if catalog.skills[name].static} == {"activity-checklist"}


async def test_the_model_is_told_the_kinds_of_activity(app: FastAPI) -> None:
    """Where it writes one, so a guess doesn't cost the person a call of their day's AI."""
    tools = app.state.agents.catalog.tools
    kinds = list(rules().activities.kinds)

    for name in ("travel_day_draft", "travel_change_draft"):
        parameters = tools[name].definition()["function"]["parameters"]
        assert parameters["$defs"]["AiActivity"]["properties"]["kind"]["enum"] == kinds, name


async def test_ai_runs_only_on_a_basic_trip(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = await register(client)
    static = await add_trip(client, account)
    monkeypatch.setattr(trips, "premium_allowed", lambda _ctx: True)
    premium = await add_trip(client, account, mode="premium")

    for trip in (static, premium):
        refused = await post(client, account, f"{TRIPS}/{trip['id']}/assist", task="ask", text="?")
        assert (refused.status_code, refused.json()["code"]) == (409, "TRAVEL_AI_OFF")
        assert refused.json()["details"] == {"mode": trip["mode"]}
    assert await runs_of(account) == 0  # refused before any run


async def test_a_request_is_checked_before_any_run_starts(client: AsyncClient) -> None:
    account = await register(client)
    stranger = await register(client)
    trip = await add_trip(client, account, mode="basic")
    other = await add_trip(client, account, mode="basic")
    elsewhere = await add_activity(client, account, other["id"])
    done = await add_activity(client, account, trip["id"])
    assert (await change(client, account, done["id"], action="done", apply=True)).is_success
    url = f"{TRIPS}/{trip['id']}/assist"

    answers = {
        "day outside": await post(client, account, url, task="day", day="2030-04-01"),
        "other trip's": await post(
            client, account, url, task="activity", activity_id=elsewhere["id"]
        ),
        "not planned": await post(
            client, account, url, task="change", activity_id=done["id"], text="x"
        ),
        "no day": await post(client, account, url, task="day"),
        "no words": await post(client, account, url, task="change", activity_id=done["id"]),
        "not theirs": await post(client, stranger, url, task="ask", text="what's on?"),
    }

    codes = {name: (answer.status_code, answer.json()["code"]) for name, answer in answers.items()}
    assert codes == {
        "day outside": (422, "TRAVEL_DAY_OUTSIDE_TRIP"),
        "other trip's": (422, "TRAVEL_ACTIVITY_UNKNOWN"),
        "not planned": (409, "TRAVEL_ACTIVITY_STATE"),
        "no day": (422, "VALIDATION_ERROR"),
        "no words": (422, "VALIDATION_ERROR"),
        "not theirs": (404, "NOT_FOUND"),
    }
    assert await runs_of(account) == await runs_of(stranger) == 0


# ---------------------------------------------------------------------- each task, end to end


@respx.mock
async def test_a_day_is_drafted_previewed_applied_and_undone(agent_client: AgentClient) -> None:
    """Use-case test 7: a structured day of several activities, beside the day's own."""
    drafted: list[dict[str, Any]] = [
        {"title": "Old Goa churches", "kind": "sightseeing", "location": "Old Goa"},
        {"title": "Spice farm", "kind": "tour", "duration_minutes": 180},
        {"title": "Sunset at the beach", "kind": "beach", "estimated_cost": 500},
    ]
    titles = [item["title"] for item in drafted]
    async with agent_client(**AI) as client:
        account = await register(client)
        await give_consent(client, account)
        trip = await add_trip(client, account, mode="basic", interests=["history"])
        await add_activity(client, account, trip["id"], day=SECOND_DAY, title="Breakfast")
        day_draft = {"trip_id": trip["id"], "day": SECOND_DAY, "activities": drafted}
        route = respx.post(LLM_URL).mock(
            side_effect=[
                calls(("travel_trip", {"trip_id": trip["id"]})),
                calls(("travel_day", {"trip_id": trip["id"], "day": SECOND_DAY})),
                calls(("travel_day_draft", day_draft)),
                reply("I drafted three activities for your second day."),
            ]
        )
        run_id = await assist(client, account, trip["id"], task="day", day=SECOND_DAY)
        run = await finished(client, account, run_id)
        [action] = run["client_actions"]
        url = f"{TRIPS}/{trip['id']}/activities/draft"
        drafts = as_applied(action["arguments"]["activities"], trip["currency"])
        body = {"day": SECOND_DAY, "activities": drafts}
        before = await stored(account)
        preview = await post(client, account, url, **body, ai_run_id=run_id)
        previewed = await stored(account)
        applied = await post(client, account, url, **body, ai_run_id=run_id, apply=True)
        day = await timeline(client, account, trip["id"], SECOND_DAY)
        [event, *_] = await history(client, account, trip["id"])
        kept = await stored(account)
        switched = await client.patch(
            f"{TRIPS}/{trip['id']}",
            headers=account.headers,
            json={"mode": "static", "revision_no": 1},
        )
        after_switch = await stored(account)
        undone = await undo(client, account, applied.json()["event_id"])
        day_undone = await timeline(client, account, trip["id"], SECOND_DAY)

    assert route.call_count == 4
    assert (run["agent"], run["skill"], run["status"], run["mode"]) == (
        "travel",
        "plan-day",
        "completed",
        "agent",
    )
    assert run["stages"] == [
        "understanding",
        "reading",
        "checking",
        "reading",
        "checking",
        "drafting",
        "checking",
        "done",
    ]
    assert run["message"] == "I drafted three activities for your second day."
    assert action["tool"] == "travel_day_draft"
    assert [item["title"] for item in action["arguments"]["activities"]] == titles
    # The preview writes nothing; the day's own stay first, the drafted ones come after them.
    assert preview.status_code == 200, preview.text
    assert preview.json()["event_id"] is None
    lines = [(line["op"], line["title"], line["day"]) for line in preview.json()["what_changed"]]
    assert lines == [("add", title, SECOND_DAY) for title in titles]
    assert previewed == before
    assert applied.status_code == 200, applied.text
    assert day == ["Breakfast", *titles]
    assert (event["kind"], event["source"], event["ai_run_id"], event["summary"]) == (
        "day_drafted",
        "ai",
        run_id,
        {"day": SECOND_DAY, "count": 3},
    )
    made = [row for row in kept["travel_activities"] if row["title"] in titles]
    assert {(row["confirmed"], row["booking_id"]) for row in made} == {(False, None)}
    ids = {row["_id"] for row in made}
    revisions = [row for row in kept["travel_activity_revisions"] if row["entity_id"] in ids]
    assert {row["source"] for row in revisions} == {"ai"}
    # Switching mode keeps every record, AI-made ones included; undo still works.
    assert switched.status_code == 200, switched.text
    for name in ("travel_activities", "travel_activity_revisions", "travel_events"):
        assert after_switch[name] == kept[name], name
    assert undone.status_code == 200, undone.text
    assert day_undone == ["Breakfast"]


@respx.mock
async def test_money_is_in_the_currencys_main_unit_both_ways(agent_client: AgentClient) -> None:
    """In yen, which has no minor unit: the model never has to know a currency's decimals."""
    async with agent_client(**AI) as client:
        account = await register(client)
        await give_consent(client, account)
        trip = await add_trip(client, account, mode="basic", currency="JPY")
        await add_activity(client, account, trip["id"], estimated_cost_minor=1500)
        dinner = {"title": "Dinner", "kind": "food", "estimated_cost": 5000}
        route = respx.post(LLM_URL).mock(
            side_effect=[
                calls(("travel_day", {"trip_id": trip["id"], "day": FIRST_DAY})),
                calls(
                    (
                        "travel_day_draft",
                        {"trip_id": trip["id"], "day": FIRST_DAY, "activities": [dinner]},
                    )
                ),
                reply("Dinner it is."),
            ]
        )
        run_id = await assist(client, account, trip["id"], task="day", day=FIRST_DAY)
        [action] = (await finished(client, account, run_id))["client_actions"]
        [drafted] = action["arguments"]["activities"]
        preview = await post(
            client,
            account,
            f"{TRIPS}/{trip['id']}/activities/draft",
            day=FIRST_DAY,
            activities=as_applied([drafted], "JPY"),
            ai_run_id=run_id,
        )

    [day_read] = tool_results(route, 1)
    assert day_read["result"]["currency"] == "JPY"
    assert day_read["result"]["activities"][0]["estimated_cost"] == 1500  # ¥1,500, never ¥15
    assert drafted["estimated_cost"] == 5000  # as the model said it, for the page to convert
    [line] = preview.json()["what_changed"]
    assert {item["field"]: item["new"] for item in line["fields"]}["estimated_cost_minor"] == 5000


@respx.mock
async def test_a_runs_day_draft_is_added_once_until_undone(agent_client: AgentClient) -> None:
    """A retried apply, or one from another tab, never adds the same activities twice."""
    activities = [{"title": "Fort", "kind": "sightseeing"}]
    async with agent_client(**AI) as client:
        account = await register(client)
        await give_consent(client, account)
        trip = await add_trip(client, account, mode="basic")
        draft = {"trip_id": trip["id"], "day": FIRST_DAY, "activities": activities}
        respx.post(LLM_URL).mock(side_effect=[calls(("travel_day_draft", draft)), reply("Fort.")])
        run_id = await assist(client, account, trip["id"], task="day", day=FIRST_DAY)
        await finished(client, account, run_id)
        url = f"{TRIPS}/{trip['id']}/activities/draft"
        body = {"day": FIRST_DAY, "activities": activities, "ai_run_id": run_id, "apply": True}
        applied = await post(client, account, url, **body)
        again = await post(client, account, url, **body)
        day = await timeline(client, account, trip["id"], FIRST_DAY)
        await undo(client, account, applied.json()["event_id"])
        after_undo = await post(client, account, url, **body)

    assert applied.status_code == 200, applied.text
    assert (again.status_code, again.json()["code"]) == (409, "TRAVEL_AI_DRAFT_APPLIED")
    assert again.json()["details"] == {"ai_run_id": run_id}
    assert day == ["Fort"]
    assert after_undo.status_code == 200, after_undo.text


@respx.mock
async def test_an_activity_gets_its_own_items_as_the_assistants(agent_client: AgentClient) -> None:
    """Use-case test 8: items for that one activity, never overwriting, a removed one gone."""
    async with agent_client(**AI) as client:
        account = await register(client)
        await give_consent(client, account)
        trip = await add_trip(client, account, mode="basic")
        hike = await add_activity(client, account, trip["id"], day=SECOND_DAY, kind="hike")
        beach = await add_activity(client, account, trip["id"], day=SECOND_DAY)
        await add_item(client, account, trip["id"], title="Walking poles", activity_id=hike["id"])
        items = [
            {"title": "Head torch", "bag": "day_bag"},
            {"title": "Blister plasters", "bag": "day_bag", "quantity": 2},
            {"title": "head  TORCH!"},  # the same item, worded otherwise: added once
        ]
        draft = {"trip_id": trip["id"], "activity_id": hike["id"], "items": items}
        respx.post(LLM_URL).mock(
            side_effect=[
                calls(("travel_activity", {"activity_id": hike["id"]})),
                calls(("travel_items_draft", draft)),
                reply("Two things for the hike."),
            ]
        )
        run_id = await assist(client, account, trip["id"], task="activity", activity_id=hike["id"])
        run = await finished(client, account, run_id)
        [action] = run["client_actions"]
        url = f"{TRIPS}/{trip['id']}/checklist/assist"
        body = {
            "activity_id": hike["id"],
            "items": action["arguments"]["items"],
            "ai_run_id": run_id,
        }
        preview = await post(client, account, url, **body)
        applied = await post(client, account, url, **body, apply=True)
        listed = await items_of(client, account, trip["id"], f"?activity_id={hike['id']}")
        [event, *_] = await history(client, account, trip["id"])
        undone = await undo(client, account, applied.json()["event_id"])
        again = await post(client, account, url, **body, apply=True)
        torch = next(
            row
            for row in await items_of(client, account, trip["id"])
            if row["title"] == "Head torch"
        )
        removed = await client.delete(f"{TRAVEL}/checklist/{torch['id']}", headers=account.headers)
        once_more = await post(client, account, url, **body, apply=True)
        beach_items = await items_of(client, account, trip["id"], f"?activity_id={beach['id']}")

    keys = [f"ai:{hike['id']}:head_torch", f"ai:{hike['id']}:blister_plasters"]
    assert [line["title"] for line in preview.json()["what_changed"]] == [
        "Head torch",
        "Blister plasters",
    ]
    assert preview.json()["event_id"] is None
    assert applied.status_code == 200, applied.text
    ai_items = [row for row in listed if row["source"] == "assistant"]
    assert [(row["suggestion_key"], row["day"], row["bag"]) for row in ai_items] == [
        (keys[0], SECOND_DAY, "day_bag"),
        (keys[1], SECOND_DAY, "day_bag"),
    ]
    assert [row["quantity"] for row in ai_items] == [1, 2]
    assert not any(row["as_suggested"] for row in ai_items)  # not the app's own rule words
    assert next(row for row in listed if row["title"] == "Walking poles")["source"] == "user"
    assert (event["kind"], event["source"], event["ai_run_id"]) == (
        "checklist_ai_suggested",
        "ai",
        run_id,
    )
    assert event["summary"] == {
        "count": 2,
        "title": hike["title"],
        "day": SECOND_DAY,
        "activity_id": hike["id"],
    }
    assert undone.status_code == 200, undone.text
    assert len(again.json()["what_changed"]) == 2  # what an undo took back can be offered again
    assert removed.status_code == 200, removed.text
    # Rule 4: one there is never touched, and one the person removed doesn't come back.
    assert (once_more.json()["what_changed"], once_more.json()["event_id"]) == ([], None)
    assert beach_items == []  # nothing for any other activity


@respx.mock
async def test_what_changed_goes_through_the_usual_change(agent_client: AgentClient) -> None:
    async with agent_client(**AI) as client:
        account = await register(client)
        await give_consent(client, account)
        trip = await add_trip(client, account, mode="basic")
        boat = await add_activity(client, account, trip["id"], title="Boat ride", kind="tour")
        await add_item(client, account, trip["id"], title="Cap", activity_id=boat["id"])
        skip = {"action": "skip", "actual_note": "It rained"}
        draft = {"trip_id": trip["id"], "activity_id": boat["id"], "change": skip}
        respx.post(LLM_URL).mock(
            side_effect=[
                calls(("travel_activity", {"activity_id": boat["id"]})),
                calls(("travel_change_draft", draft)),
                reply("Marked to skip the boat ride: check the preview."),
            ]
        )
        run_id = await assist(
            client,
            account,
            trip["id"],
            task="change",
            activity_id=boat["id"],
            text="we skipped it, rain",
        )
        run = await finished(client, account, run_id)
        [action] = run["client_actions"]
        sent_change = {
            key: value for key, value in action["arguments"]["change"].items() if value is not None
        }
        before = await stored(account)
        preview = await change(client, account, boat["id"], **sent_change, ai_run_id=run_id)
        previewed = await stored(account)
        applied = await change(
            client, account, boat["id"], **sent_change, ai_run_id=run_id, apply=True
        )
        [event, *_] = await history(client, account, trip["id"])
        undone = await undo(client, account, applied.json()["event_id"])

    assert (run["status"], run["skill"], action["tool"]) == (
        "completed",
        "update-trip",
        "travel_change_draft",
    )
    assert sent_change == skip
    assert preview.status_code == 200, preview.text
    assert previewed == before
    assert {line["title"] for line in preview.json()["what_changed"]} == {"Boat ride", "Cap"}
    assert applied.status_code == 200, applied.text
    assert (event["kind"], event["source"], event["ai_run_id"]) == (
        "activity_skipped",
        "ai",
        run_id,
    )
    assert undone.status_code == 200, undone.text


@respx.mock
async def test_describing_a_trip_drafts_the_steps_and_saves_nothing(
    agent_client: AgentClient,
) -> None:
    """Use-case test 3, with a fake model: the draft validates as the steps would save it."""
    draft = {
        "title": "Goa with friends",
        "destinations": [{"name": "Goa", "country": "India", "timezone": "Asia/Kolkata"}],
        "start_date": "2030-03-03",
        "end_date": "2030-03-07",
        "trip_type": "friends",
        "pace": "relaxed",
        "budget": 20000,  # as the person said it: ₹20,000
        "currency": "INR",
        "interests": ["beaches", "seafood"],
    }
    route = respx.post(LLM_URL).mock(
        side_effect=[
            calls(("travel_trip_draft", {"trip": draft})),
            reply("I filled in Goa with friends, 3 to 7 March."),
        ]
    )
    async with agent_client(**AI) as client:
        account = await register(client)
        await give_consent(client, account)
        answer = await post(
            client, account, f"{TRAVEL}/assist", text="Goa with friends 3-7 March 2030, relaxed"
        )
        run = await finished(client, account, answer.json()["run_id"])
        nothing = await stored(account)
        [action] = run["client_actions"]
        # What the planning steps then save: the budget in minor units of its currency.
        steps = dict(action["arguments"]["trip"])
        budget_minor = to_minor(steps.pop("budget"), steps["currency"])
        saved = await post(client, account, TRIPS, **steps, budget_minor=budget_minor)

    assert answer.status_code == 202, answer.text
    assert action["arguments"]["trip"]["budget"] == 20000
    assert (run["skill"], run["status"], action["tool"]) == (
        "describe-trip",
        "completed",
        "travel_trip_draft",
    )
    assert route.call_count == 2
    assert all(rows == [] for rows in nothing.values())  # the run saved no trip
    assert saved.status_code == 201, saved.text
    assert (saved.json()["title"], saved.json()["start_date"]) == ("Goa with friends", "2030-03-03")
    assert saved.json()["budget_minor"] == 2000000


@respx.mock
async def test_a_draft_is_checked_as_the_persons_own_input(agent_client: AgentClient) -> None:
    backwards = {
        "title": "Goa",
        "destinations": [{"name": "Goa", "country": "India"}],
        "start_date": "2030-03-07",
        "end_date": "2030-03-03",
    }
    async with agent_client(**AI) as client:
        account = await register(client)
        await give_consent(client, account)
        trip = await add_trip(client, account, mode="basic")
        booked = {"title": "Ferry", "kind": "transport", "confirmed": True}
        guessed = {"title": "Museum", "kind": "museum"}
        route = respx.post(LLM_URL).mock(
            side_effect=[
                calls(("travel_trip_draft", {"trip": backwards})),
                reply("Which dates do you mean?"),
                calls(
                    (
                        "travel_day_draft",
                        {"trip_id": trip["id"], "day": LAST_DAY, "activities": [booked, guessed]},
                    )
                ),
                reply("I couldn't draft that."),
            ]
        )
        described = await finished(
            client,
            account,
            (await post(client, account, f"{TRAVEL}/assist", text="Goa, 7 to 3 March")).json()[
                "run_id"
            ],
        )
        day = await finished(
            client, account, await assist(client, account, trip["id"], task="day", day=LAST_DAY)
        )

    [dates] = tool_results(route, 1)
    [refused] = tool_results(route, 3)
    assert dates == {"error": "TRAVEL_TRIP_DATES_INVALID"}
    assert refused["error"] == "invalid_arguments"
    confirmed, kind = refused["details"]
    assert (confirmed["loc"], confirmed["type"]) == (
        ["activities", 0, "confirmed"],
        "extra_forbidden",
    )
    # A kind it guessed: told which there are, so the next try can be right.
    assert kind["loc"] == ["activities", 1, "kind"]
    assert ", ".join(rules().activities.kinds) in kind["msg"]
    assert (described["client_actions"], described["message"]) == ([], "Which dates do you mean?")
    assert day["client_actions"] == []


@respx.mock
async def test_a_question_is_answered_from_the_trips_records(agent_client: AgentClient) -> None:
    async with agent_client(**AI) as client:
        account = await register(client)
        await give_consent(client, account)
        trip = await add_trip(client, account, mode="basic", budget_minor=2000000, currency="INR")
        route = respx.post(LLM_URL).mock(
            side_effect=[
                calls(("travel_budget", {"trip_id": trip["id"]})),
                calls(("travel_open", {"trip_id": trip["id"], "section": "budget"})),
                reply("Nothing is spent yet: all ₹20,000 is left."),
            ]
        )
        run = await finished(
            client,
            account,
            await assist(client, account, trip["id"], task="ask", text="how much is left?"),
        )

    [budget] = tool_results(route, 1)
    assert budget["result"] == {  # as people say it: ₹20,000, never 2000000 paise
        "currency": "INR",
        "budget": 20000,
        "estimated": 0,
        "actual": 0,
        "remaining": 20000,
        "categories": [],
        "unconverted": 0,
    }
    assert (run["skill"], run["message"]) == (
        "ask-trip",
        "Nothing is spent yet: all ₹20,000 is left.",
    )
    assert run["client_actions"] == [
        {
            "tool": "travel_open",
            "arguments": {"trip_id": trip["id"], "section": "budget", "day": None},
        }
    ]


# ---------------------------------------------------------------------- the run an apply names


@respx.mock
async def test_an_apply_names_only_the_callers_own_ai_draft_for_that_trip(
    agent_client: AgentClient,
) -> None:
    async with agent_client(**AI) as client:
        account = await register(client)
        stranger = await register(client)
        await give_consent(client, account)
        trip = await add_trip(client, account, mode="basic")
        other = await add_trip(client, account, mode="basic")
        theirs = await add_trip(client, stranger, mode="basic")
        hike = await add_activity(client, stranger, theirs["id"], kind="hike")
        activities = [{"title": "Fort", "kind": "sightseeing"}]
        respx.post(LLM_URL).mock(
            side_effect=[
                calls(
                    (
                        "travel_day_draft",
                        {"trip_id": trip["id"], "day": FIRST_DAY, "activities": activities},
                    )
                ),
                reply("Drafted."),
            ]
        )
        run_id = await assist(client, account, trip["id"], task="day", day=FIRST_DAY)
        assert (await finished(client, account, run_id))["status"] == "completed"
        # The stranger has no consent: their activity's help falls back to the rules.
        static_id = await assist(
            client, stranger, theirs["id"], task="activity", activity_id=hike["id"]
        )
        assert (await finished(client, stranger, static_id))["mode"] == "static"

        def day(trip_id: str) -> str:
            return f"{TRIPS}/{trip_id}/activities/draft"

        draft = {"day": FIRST_DAY, "activities": activities}
        items = {"activity_id": hike["id"], "items": [{"title": "Hat"}]}
        answers = {
            "its own": await post(client, account, day(trip["id"]), **draft, ai_run_id=run_id),
            "another trip": await post(
                client, account, day(other["id"]), **draft, ai_run_id=run_id
            ),
            "another kind": await post(
                client,
                account,
                f"{TRIPS}/{trip['id']}/checklist/assist",
                activity_id=hike["id"],
                items=[{"title": "Hat"}],
                ai_run_id=run_id,
            ),
            "not theirs": await post(
                client, stranger, day(theirs["id"]), **draft, ai_run_id=run_id
            ),
            "no such run": await post(client, account, day(trip["id"]), **draft, ai_run_id="nope"),
            "no AI in it": await post(
                client,
                stranger,
                f"{TRIPS}/{theirs['id']}/checklist/assist",
                **items,
                ai_run_id=static_id,
            ),
        }
        unchanged = await stored(account)

    codes = {
        name: (answer.status_code, answer.json().get("code")) for name, answer in answers.items()
    }
    assert codes == {
        "its own": (200, None),
        "another trip": (422, "TRAVEL_AI_RUN_INVALID"),
        "another kind": (422, "TRAVEL_AI_RUN_INVALID"),
        "not theirs": (422, "TRAVEL_AI_RUN_INVALID"),
        "no such run": (422, "TRAVEL_AI_RUN_INVALID"),
        "no AI in it": (422, "TRAVEL_AI_RUN_INVALID"),
    }
    assert len(unchanged["travel_activities"]) == 0  # previews and refusals wrote nothing


async def test_without_ai_an_activity_is_offered_the_rule_suggestions(client: AsyncClient) -> None:
    """The one honest fallback: the app's own rule suggestions for that activity, as such."""
    account = await register(client)  # no consent to AI
    trip = await add_trip(client, account, mode="basic")
    hike = await add_activity(client, account, trip["id"], kind="hike")
    await add_activity(client, account, trip["id"], kind="beach")
    rest = await add_activity(client, account, trip["id"], kind="rest")

    run = await finished(
        client,
        account,
        await assist(client, account, trip["id"], task="activity", activity_id=hike["id"]),
    )
    none = await finished(
        client,
        account,
        await assist(client, account, trip["id"], task="activity", activity_id=rest["id"]),
    )
    offered = await suggest(client, account, trip["id"], activity_id=hike["id"])
    other = await add_trip(client, account)
    elsewhere = await suggest(client, account, other["id"], activity_id=hike["id"])

    params = {"trip_id": trip["id"], "activity_id": hike["id"], "day": FIRST_DAY}
    assert (run["status"], run["mode"], run["reason"]) == ("completed", "static", "consent")
    assert (run["message_code"], run["message_params"]) == ("activity-checklist.rules", params)
    assert run["client_actions"] == []
    assert none["message_code"] == "activity-checklist.no_rules"
    keys = {line["suggestion_key"] for line in offered.json()["what_changed"]}
    assert keys == {f"activity:{hike['id']}:{rule.key}" for rule in HIKE_RULES}  # its own only
    assert (elsewhere.status_code, elsewhere.json()["code"]) == (422, "TRAVEL_ACTIVITY_UNKNOWN")


@pytest.mark.parametrize("task", ["day", "change", "ask"])
async def test_without_ai_the_other_tasks_say_so(client: AsyncClient, task: str) -> None:
    account = await register(client)
    trip = await add_trip(client, account, mode="basic")
    activity = await add_activity(client, account, trip["id"])
    asked = {"day": FIRST_DAY, "activity_id": activity["id"], "text": "what now?"}

    run = await finished(
        client, account, await assist(client, account, trip["id"], task=task, **asked)
    )

    assert (run["status"], run["mode"], run["reason"]) == ("unavailable", None, "consent")
    assert run["client_actions"] == []
