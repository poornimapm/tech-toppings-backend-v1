"""``/v1/agents``: modes and fallback, the agent loop, quotas, and confirming proposals."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from httpx import AsyncClient

from app.core.config import AgentSettings
from app.platform.modules.models import ModuleOverride
from tests.helpers import Account, FrozenClock, register
from tests.platform.agents.conftest import (
    LLM_URL,
    PRIMARY,
    AgentClient,
    ask,
    calls,
    give_consent,
    reply,
    sent,
)

PIN_HABITS = ("modules_set_preferences", {"key": "habits", "pinned": True, "summary": "Pin Habits"})


def agent_conversation() -> list[httpx.Response]:
    """load the skill -> list modules -> propose a change -> reply."""
    return [
        calls(("load_skill", {"name": "manage-modules"})),
        calls(("modules_list", {})),
        calls(PIN_HABITS),
        reply("I've prepared that: confirm to pin Habit tracker."),
    ]


# ------------------------------------------------------------------ modes and fallback


async def test_without_consent_static_mode_answers_by_default(agent_client: AgentClient) -> None:
    async with agent_client(providers=[PRIMARY]) as client:
        account = await register(client)
        agents = (await client.get("/v1/agents", headers=account.headers)).json()["items"]
        run = await ask(client, account, "pin habits")

    assert agents[0]["mode"] == "static"
    assert agents[0]["reason"] == "consent"
    assert agents[0]["skills"] == [
        {
            "name": "manage-modules",
            "description": agents[0]["skills"][0]["description"],
            "mode": "agent",
            "has_static": True,
        }
    ]
    assert (run["mode"], run["reason"], run["status"]) == ("static", "consent", "completed")
    assert run["message_code"] == "manage-modules.ready.pin"
    assert run["proposals"][0]["summary_code"] == "manage-modules.change.pin"


async def test_with_fallback_off_it_says_honestly_that_ai_is_unavailable(
    agent_client: AgentClient,
) -> None:
    async with agent_client(fallback=False) as client:
        account = await register(client)
        await give_consent(client, account)
        run = await ask(client, account, "pin habits")

    assert (run["status"], run["mode"], run["reason"]) == ("unavailable", None, "no_provider")
    assert run["proposals"] == []
    assert run["message"] is None


@respx.mock
async def test_static_mode_never_calls_a_provider(agent_client: AgentClient) -> None:
    route = respx.post(LLM_URL).mock(return_value=reply("unused"))
    async with agent_client(mode="static", providers=[PRIMARY]) as client:
        account = await register(client)
        await give_consent(client, account)
        run = await ask(client, account, "hide journal")

    assert (run["mode"], run["reason"]) == ("static", None)
    assert route.call_count == 0


@respx.mock
async def test_a_skill_can_be_switched_to_static_on_its_own(agent_client: AgentClient) -> None:
    route = respx.post(LLM_URL).mock(return_value=reply("unused"))
    async with agent_client(providers=[PRIMARY], skill_modes="manage-modules=static") as client:
        account = await register(client)
        await give_consent(client, account)
        run = await ask(client, account, "pin habits", skill="manage-modules")

    assert run["mode"] == "static"
    assert route.call_count == 0


@respx.mock
async def test_when_every_provider_fails_the_switches_decide(agent_client: AgentClient) -> None:
    respx.post(LLM_URL).mock(return_value=httpx.Response(503))
    async with agent_client(providers=[PRIMARY], fallback=True) as client:
        account = await register(client)
        await give_consent(client, account)
        fell_back = await ask(client, account, "pin habits")
    async with agent_client(providers=[PRIMARY], fallback=False) as client:
        account = await register(client)
        await give_consent(client, account)
        honest = await ask(client, account, "pin habits")

    assert (fell_back["mode"], fell_back["reason"]) == ("static", "providers_failed")
    assert (honest["status"], honest["reason"]) == ("unavailable", "providers_failed")


async def test_unrelated_requests_get_an_honest_no_match_in_static_mode(
    agent_client: AgentClient,
) -> None:
    async with agent_client(mode="static") as client:
        account = await register(client)
        run = await ask(client, account, "what is the weather like")

    assert (run["status"], run["message_code"], run["proposals"]) == (
        "completed",
        "static.no_match",
        [],
    )


# ------------------------------------------------------------------ the agent loop


@respx.mock
async def test_agent_loads_the_skill_reads_and_proposes(ai_client: AsyncClient) -> None:
    route = respx.post(LLM_URL).mock(side_effect=agent_conversation())
    account = await register(ai_client)
    await give_consent(ai_client, account)

    run = await ask(
        ai_client,
        account,
        "please pin habits, mail me at asha@example.com",
        context={"modules": [{"key": "habits", "name": "Habit tracker"}]},
    )

    assert (run["mode"], run["status"], run["skill"]) == ("agent", "completed", "manage-modules")
    assert run["message"] == "I've prepared that: confirm to pin Habit tracker."
    (proposal,) = run["proposals"]
    assert (proposal["tool"], proposal["arguments"]) == (
        "modules_set_preferences",
        {"key": "habits", "pinned": True, "enabled": None},
    )
    assert proposal["summary"] == "Pin Habits"
    first = sent(route, 0)
    system, user = first["messages"][0]["content"], first["messages"][-1]["content"]
    assert "{{" not in system  # every placeholder filled
    assert "manage-modules:" in system
    assert "Habit tracker" in system
    assert "asha@example.com" not in user
    assert "[redacted]" in user
    assert [tool["function"]["name"] for tool in first["tools"]] == ["load_skill"]  # disclosure
    assert "modules_list" in {tool["function"]["name"] for tool in sent(route, 1)["tools"]}
    stored = (await ai_client.get(f"/v1/agents/runs/{run['id']}", headers=account.headers)).json()
    assert stored["id"] == run["id"]


@respx.mock
async def test_model_mistakes_are_recorded_not_fatal(ai_client: AsyncClient) -> None:
    respx.post(LLM_URL).mock(
        side_effect=[
            calls(("modules_list", {})),  # not loaded yet
            calls(
                ("load_skill", {"name": "nope"}), ("no_such_tool", {}), ("load_skill", "{broken")
            ),
            reply("Sorry, I couldn't do that."),
        ]
    )
    account = await register(ai_client)
    await give_consent(ai_client, account)

    run = await ask(ai_client, account, "pin habits")

    assert run["status"] == "completed"
    assert run["proposals"] == []


@respx.mock
async def test_the_step_limit_stops_a_looping_model(agent_client: AgentClient) -> None:
    route = respx.post(LLM_URL).mock(return_value=calls(("load_skill", {"name": "manage-modules"})))
    async with agent_client(providers=[PRIMARY], max_steps=3) as client:
        account = await register(client)
        await give_consent(client, account)
        run = await ask(client, account, "pin habits")

    assert run["status"] == "incomplete"
    assert route.call_count == 3


@respx.mock
async def test_open_becomes_a_client_action(ai_client: AsyncClient) -> None:
    respx.post(LLM_URL).mock(
        side_effect=[
            calls(("load_skill", {"name": "manage-modules"})),
            calls(("modules_open", {"key": "journal"})),
            reply("Opening Journal."),
        ]
    )
    account = await register(ai_client)
    await give_consent(ai_client, account)

    run = await ask(ai_client, account, "open my journal")

    assert run["client_actions"] == [{"tool": "modules_open", "arguments": {"key": "journal"}}]


# ------------------------------------------------------------------ quotas


@respx.mock
async def test_quota_counts_every_ai_call(agent_client: AgentClient) -> None:
    respx.post(LLM_URL).mock(side_effect=[*agent_conversation(), reply("unused")])
    async with agent_client(providers=[PRIMARY], user_daily_calls=4) as client:
        account = await register(client)
        await give_consent(client, account)
        first = await ask(client, account, "pin habits")
        agents = (await client.get("/v1/agents", headers=account.headers)).json()["items"]
        second = await ask(client, account, "pin habits")

    assert first["mode"] == "agent"
    assert agents[0]["quota_remaining"] == 0
    assert (agents[0]["mode"], agents[0]["reason"]) == ("static", "quota")
    assert (second["mode"], second["reason"]) == ("static", "quota")


@respx.mock
async def test_running_out_mid_run_keeps_what_was_done(agent_client: AgentClient) -> None:
    respx.post(LLM_URL).mock(side_effect=agent_conversation())
    async with agent_client(providers=[PRIMARY], user_daily_calls=3) as client:
        account = await register(client)
        await give_consent(client, account)
        run = await ask(client, account, "pin habits")

    assert (run["status"], run["reason"]) == ("incomplete", "quota")
    assert len(run["proposals"]) == 1  # the proposal made before the limit is kept


async def test_a_zero_global_budget_switches_ai_off(agent_client: AgentClient) -> None:
    async with agent_client(providers=[PRIMARY], global_daily_calls=0, fallback=False) as client:
        account = await register(client)
        await give_consent(client, account)
        run = await ask(client, account, "pin habits")

    assert (run["status"], run["reason"]) == ("unavailable", "quota")


# ------------------------------------------------------------------ proposals


async def static_proposal(
    client: AsyncClient, text: str = "pin habits"
) -> tuple[Account, dict[str, Any]]:
    account = await register(client)
    run = await ask(client, account, text)
    return account, run["proposals"][0]


async def test_confirming_runs_the_change_once(agent_client: AgentClient) -> None:
    async with agent_client(mode="static") as client:
        account, proposal = await static_proposal(client)
        headers = account.headers
        confirmed = await client.post(
            f"/v1/agents/proposals/{proposal['id']}/confirm", headers=headers
        )
        again = await client.post(f"/v1/agents/proposals/{proposal['id']}/confirm", headers=headers)
        habits = (await client.get("/v1/modules/habits", headers=headers)).json()

    assert confirmed.json()["status"] == "confirmed"
    assert confirmed.json()["result"]["pinned"] is True
    assert habits["pinned"] is True
    assert again.status_code == 409
    assert again.json()["code"] == "AGENT_PROPOSAL_CLOSED"


async def test_rejecting_changes_nothing(agent_client: AgentClient) -> None:
    async with agent_client(mode="static") as client:
        account, proposal = await static_proposal(client)
        headers = account.headers
        rejected = await client.post(
            f"/v1/agents/proposals/{proposal['id']}/reject", headers=headers
        )
        habits = (await client.get("/v1/modules/habits", headers=headers)).json()

    assert rejected.json()["status"] == "rejected"
    assert habits["pinned"] is False


async def test_proposals_expire(agent_client: AgentClient, clock: FrozenClock) -> None:
    async with agent_client(mode="static", proposal_ttl_minutes=5) as client:
        account, proposal = await static_proposal(client)
        clock.advance(minutes=6)
        late = await client.post(
            f"/v1/agents/proposals/{proposal['id']}/confirm", headers=account.headers
        )

    assert late.status_code == 409


async def test_a_change_that_fails_is_reported(agent_client: AgentClient) -> None:
    async with agent_client(mode="static") as client:
        account, proposal = await static_proposal(client, "notify me when habits launches")
        now = FrozenClock().now()  # meanwhile an admin launches the module: no notice possible
        await ModuleOverride(key="habits", status="live", created_at=now, updated_at=now).insert()
        try:
            result = await client.post(
                f"/v1/agents/proposals/{proposal['id']}/confirm", headers=account.headers
            )
        finally:
            await ModuleOverride.get_pymongo_collection().delete_many({})

    assert result.json()["status"] == "failed"
    assert result.json()["error_code"] == "MODULE_NOT_UPCOMING"


# ------------------------------------------------------------------ input checks


@pytest.mark.parametrize(
    ("path", "body", "status", "code"),
    [
        ("/v1/agents/nobody/runs", {"input": "hi"}, 404, "AGENT_NOT_FOUND"),
        (
            "/v1/agents/assistant/runs",
            {"input": "hi", "skill": "nope"},
            404,
            "AGENT_SKILL_NOT_FOUND",
        ),
        ("/v1/agents/assistant/runs", {"input": "x" * 50}, 422, "AGENT_INPUT_TOO_LONG"),
        ("/v1/agents/assistant/runs", {"input": ""}, 422, "VALIDATION_ERROR"),
    ],
)
async def test_bad_requests(
    agent_client: AgentClient, path: str, body: dict[str, object], status: int, code: str
) -> None:
    async with agent_client(max_input_chars=20) as client:
        account = await register(client)
        response = await client.post(path, headers=account.headers, json=body)

    assert response.status_code == status
    assert response.json()["code"] == code


def test_defaults_keep_ai_switchable() -> None:
    settings = AgentSettings(_env_file=None)

    assert (settings.mode, settings.fallback, settings.providers) == ("agent", True, ())
