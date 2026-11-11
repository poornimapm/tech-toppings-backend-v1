"""What Basic AI must never do, with the fake provider: send the model what the plan keeps from it
(names, passport details, residence, food or access needs, notes, booking references, who paid),
show the provider's key in any answer or log line (use-case test 16), or write anything when AI is
unavailable or fails (use-case test 15)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from httpx import Response

from tests.helpers import register, unique_email
from tests.modules.travel.conftest import (
    FIRST_DAY,
    TRIPS,
    add_activity,
    add_booking,
    add_expense,
    add_item,
    add_trip,
    assist,
    change,
    finished,
    history,
    stored,
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
TRAVELLER = {
    "name": "Zaheera Quill",
    "passport_country": "Atlantis",
    "residence_country": "Narnia",
    "passport_expiry": "2033-11-27",
    "dietary": "no peanuts ever",
    "accessibility": "step-free rooms only",
}
TRIP_NOTES = "Anniversary surprise for Zaheera"
REFERENCE = "PNR-XY7Q2"
BOOKING_NOTES = "Pay the guide in cash"
BOOKING_TITLE = "Kestrelwood Charters"
BOOKING_PLACE = "Pier 9, Varrowmere Wharf"
ACTIVITY_NOTES = "Ask for Mr Bingley"
WHAT_HAPPENED = "Lost my sandal in the Mandovi"
EXPENSE_TITLE = "Quillfeather supper"
STATIC_NOTES = "A static trip's own notes"
HOLDER = "Ottoline Vex"  # the account's own name and email: never the model's


@respx.mock
async def test_the_model_never_sees_what_the_plan_keeps_from_it(agent_client: AgentClient) -> None:
    async with agent_client(**AI) as client:
        account = await register(client, email=unique_email("ottoline.vex"), name=HOLDER)
        await give_consent(client, account)
        trip = await add_trip(
            client, account, mode="basic", notes=TRIP_NOTES, travellers=[TRAVELLER]
        )
        traveller = trip["travellers"][0]["id"]
        static = await add_trip(client, account, title="Elsewhere", notes=STATIC_NOTES)
        booking = await add_booking(
            client,
            account,
            trip["id"],
            title=BOOKING_TITLE,
            location=BOOKING_PLACE,
            reference=REFERENCE,
            notes=BOOKING_NOTES,
        )
        tour = await add_activity(
            client,
            account,
            trip["id"],
            title="Island tour",
            kind="tour",
            booking_id=booking["id"],
            notes=ACTIVITY_NOTES,
            estimated_cost_minor=150000,
        )
        # Done, in the person's own words of what happened (the tour stays planned for the runs).
        boat = await add_activity(client, account, trip["id"], title="Boat ride", kind="tour")
        done = await change(
            client, account, boat["id"], action="done", actual_note=WHAT_HAPPENED, apply=True
        )
        assert done.status_code == 200, done.text
        await add_item(
            client,
            account,
            trip["id"],
            title="Sun hat",
            activity_id=tour["id"],
            traveller_id=traveller,
        )
        await add_expense(
            client,
            account,
            trip["id"],
            title=EXPENSE_TITLE,
            paid_by=traveller,
            shared_with=[traveller],
        )
        on_trip = {"trip_id": trip["id"]}
        route = respx.post(LLM_URL).mock(
            side_effect=[
                calls(
                    ("travel_trip", on_trip),
                    ("travel_day", on_trip | {"day": FIRST_DAY}),
                    ("travel_activity", {"activity_id": tour["id"]}),
                    ("travel_budget", on_trip),
                    ("travel_trip", {"trip_id": static["id"]}),  # a Static trip: refused
                ),
                reply("Here is what your trip holds."),
                calls(("travel_activity", {"activity_id": tour["id"]})),
                reply("It has what it needs."),
                calls(("travel_activity", {"activity_id": tour["id"]})),
                reply("Did it happen, or did you skip it?"),
                calls(("travel_trip", on_trip), ("travel_day", on_trip | {"day": FIRST_DAY})),
                reply("The day is full already."),
            ]
        )
        for body in (
            {"task": "ask", "text": "what does my trip hold?"},
            {"task": "activity", "activity_id": tour["id"]},
            {"task": "change", "activity_id": tour["id"], "text": "about the tour"},
            {"task": "day", "day": FIRST_DAY},
        ):
            run = await finished(client, account, await assist(client, account, trip["id"], **body))
            assert run["status"] == "completed", run

    assert route.call_count == 8
    bodies = [call.request.content.decode() for call in route.calls]
    kept_away = [
        *TRAVELLER.values(),
        HOLDER,
        account.email,
        TRIP_NOTES,
        REFERENCE,
        BOOKING_NOTES,
        BOOKING_TITLE,
        BOOKING_PLACE,
        ACTIVITY_NOTES,
        WHAT_HAPPENED,
        EXPENSE_TITLE,
        STATIC_NOTES,
        traveller,  # who paid, who an item is for
        booking["id"],
    ]
    assert [secret for secret in kept_away if any(secret in body for body in bodies)] == []
    # It did read the trip: its places, its activities and the booking's being there, its budget.
    trip_read, day_read, tour_read, budget_read, refused = [
        json.loads(m["content"]) for m in sent(route, 1)["messages"] if m["role"] == "tool"
    ]
    assert trip_read["result"]["destinations"] == [
        {"name": "Goa", "country": "India", "timezone": None}
    ]
    assert trip_read["result"]["travellers"] == 1  # how many, never who
    assert [(item["title"], item["status"]) for item in day_read["result"]["activities"]] == [
        ("Island tour", "planned"),
        ("Boat ride", "done"),
    ]
    assert tour_read["result"]["activity"]["booked"] is True
    assert budget_read["result"]["actual"] == 10  # ₹10, in the trip's main unit
    assert refused == {"error": "TRAVEL_AI_OFF"}


@respx.mock
async def test_the_providers_key_is_never_in_an_answer_or_a_log_line(
    agent_client: AgentClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """Use-case test 16."""
    key = PRIMARY.api_key.get_secret_value() if PRIMARY.api_key else ""
    activities = [{"title": "Fort", "kind": "sightseeing"}]
    answers: list[str] = []
    async with agent_client(**AI) as client:
        account = await register(client)
        await give_consent(client, account)
        trip = await add_trip(client, account, mode="basic")
        draft = {"trip_id": trip["id"], "day": FIRST_DAY, "activities": activities}
        route = respx.post(LLM_URL).mock(
            side_effect=[
                # The provider refuses, its error echoing the key it was sent.
                Response(401, json={"error": {"message": f"invalid key {key}"}}),
                calls(("travel_day_draft", draft)),
                reply("Drafted one."),
            ]
        )
        for _ in range(2):
            run_id = await assist(client, account, trip["id"], task="day", day=FIRST_DAY)
            answers.append(json.dumps(await finished(client, account, run_id)))
        applied = await client.post(
            f"{TRIPS}/{trip['id']}/activities/draft",
            headers=account.headers,
            json={"day": FIRST_DAY, "activities": activities, "ai_run_id": run_id, "apply": True},
        )
        answers += [applied.text, json.dumps(await history(client, account, trip["id"]))]
    output = capsys.readouterr()

    assert route.calls[0].request.headers["Authorization"] == f"Bearer {key}"  # it was in play
    assert '"reason": "providers_failed"' in answers[0]
    assert applied.status_code == 200, applied.text
    assert key
    assert [answer for answer in answers if key in answer] == []
    assert key not in output.out + output.err


@respx.mock
@pytest.mark.parametrize(
    ("agent", "consent", "answers", "reason"),
    [
        (AI, False, [], "consent"),
        (AI | {"user_daily_calls": 0}, True, [], "quota"),
        ({}, True, [], "no_provider"),
        (AI, True, [Response(429)], "providers_failed"),  # rate limited: it rests
        (AI, True, [Response(503)], "providers_failed"),  # a provider error
        (AI, True, [httpx.ReadTimeout("too slow")], "providers_failed"),  # a timeout
        # Failing after the run had read the trip.
        (AI, True, [calls(("travel_trip", {"trip_id": "?"})), Response(503)], "providers_failed"),
    ],
)
async def test_when_ai_is_unavailable_or_fails_nothing_is_written(
    agent_client: AgentClient,
    agent: dict[str, Any],
    consent: bool,
    answers: list[Any],
    reason: str,
) -> None:
    """Use-case test 15: the trip is just as it was, and the page can retry or go on by hand."""
    route = respx.post(LLM_URL).mock(side_effect=answers or [reply("unused")])
    async with agent_client(**agent) as client:
        account = await register(client)
        if consent:
            await give_consent(client, account)
        trip = await add_trip(client, account, mode="basic")
        await add_activity(client, account, trip["id"])
        before = await stored(account)
        run_id = await assist(client, account, trip["id"], task="day", day=FIRST_DAY)
        run = await finished(client, account, run_id)
        after = await stored(account)

    assert (run["status"], run["mode"], run["reason"]) == ("unavailable", None, reason)
    assert (run["client_actions"], run["proposals"]) == ([], [])
    assert after == before
    assert route.call_count == len(answers)  # none at all when AI was never on
