"""``/v1/expenses/insights``: a week or month in facts and a summary of them (the
explain-spending skill). An AI summary is kept while its facts are unchanged, so a second look
makes no provider call (plan, Phase 8)."""

from __future__ import annotations

import json
from typing import Any

import respx
from httpx import AsyncClient

from tests.helpers import Account
from tests.modules.expenses.conftest import add
from tests.modules.expenses.test_analytics import seeded
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

INSIGHTS = "/v1/expenses/insights"
SEPTEMBER = "period=month&date=2026-09-15"
SUMMARY = "You spent ₹4,630 in September, most of it on shoes."


async def insight(client: AsyncClient, account: Account, query: str = SEPTEMBER) -> Any:
    response = await client.get(f"{INSIGHTS}?{query}", headers=account.headers)
    assert response.status_code == 200, response.text
    return response.json()


async def test_without_ai_the_facts_are_said_with_the_skills_templates(
    agent_client: AgentClient,
) -> None:
    async with agent_client(mode="static") as client:
        account = await seeded(client)
        found = await insight(client, account)
        week = await insight(client, account, "period=week&date=2026-09-30")

    assert (found["status"], found["mode"], found["cached"]) == ("completed", "static", False)
    assert (found["start"], found["end"]) == ("2026-09-01", "2026-09-30")
    assert found["message_code"] == "explain-spending.month.more"
    assert found["message_params"] == {
        "currency": "INR",
        "spent_minor": 463000,
        "pct": 256,  # against 1-30 August
        "category": "shopping",
        "category_amount_minor": 250000,
    }
    facts = found["facts"]
    assert (facts["spent_minor"], facts["received_minor"], facts["partial"]) == (
        463000,
        5000000,
        False,
    )
    assert facts["previous"] | {"count": 0} == {
        "start": "2026-08-01",
        "end": "2026-08-30",
        "spent_minor": 130000,
        "received_minor": 5000000,
        "net_minor": 4870000,
        "count": 0,
    }
    assert [item["category_id"] for item in facts["top_categories"]] == [
        "shopping",
        "food",
        "groceries",
    ]
    assert facts["top_categories"][1]["change_pct"] == 50.0  # dinners: 800 then 1,200
    assert facts["top_merchant"]["merchant"] == "Bata"
    assert facts["people"] == [{"name": "Amma", "amount_minor": 145000}]
    assert facts["biggest"] == {"title": "Shoes", "amount_minor": 250000, "date": "2026-09-30"}
    assert facts["forecast"] is None  # September is over
    assert (week["start"], week["end"], week["message_code"]) == (
        "2026-09-28",
        "2026-10-04",
        "explain-spending.week.first",  # nothing spent the week before to compare with
    )


@respx.mock
async def test_an_ai_summary_is_kept_until_the_figures_change(agent_client: AgentClient) -> None:
    async with agent_client(providers=[PRIMARY]) as client:
        account = await seeded(client)
        await give_consent(client, account)
        route = respx.post(LLM_URL).mock(
            side_effect=[
                calls(("expenses_insight_facts", {"period": "month", "date": "2026-09-01"})),
                reply(SUMMARY),
                calls(("expenses_insight_facts", {"period": "month", "date": "2026-09-01"})),
                reply("Now with a book too."),
                calls(("expenses_insight_facts", {"period": "month", "date": "2026-09-01"})),
                reply("Written again."),
            ]
        )
        first = await insight(client, account)
        again = await insight(client, account)
        provider_calls = route.call_count
        await add(client, account, title="Book", amount_minor=40000, date="2026-09-20")
        changed = await insight(client, account)
        refreshed = await client.post(f"{INSIGHTS}/refresh?{SEPTEMBER}", headers=account.headers)

    told = [m["content"] for m in sent(route, 1)["messages"] if m["role"] == "tool"]
    facts = json.loads(told[0])["result"]
    assert (facts["spent"], facts["currency"]) == (4630.0, "INR")  # main units for the model
    assert (first["mode"], first["message"], first["cached"]) == ("agent", SUMMARY, False)
    assert (again["message"], again["cached"]) == (SUMMARY, True)
    assert provider_calls == 2  # the second look asked no one
    assert (changed["message"], changed["cached"]) == ("Now with a book too.", False)
    assert changed["facts"]["spent_minor"] == 503000
    assert refreshed.json()["message"] == "Written again."
    assert route.call_count == 6


@respx.mock
async def test_without_ai_or_fallback_it_says_so_and_still_gives_the_facts(
    agent_client: AgentClient,
) -> None:
    async with agent_client(providers=[PRIMARY], fallback=False) as client:
        account = await seeded(client)
        found = await insight(client, account)

    assert (found["status"], found["reason"], found["message"]) == ("unavailable", "consent", None)
    assert found["facts"]["spent_minor"] == 463000


async def test_the_general_assistant_sums_up_the_month_when_asked(
    agent_client: AgentClient,
) -> None:
    async with agent_client(mode="static") as client:
        account = await seeded(client)
        run = await ask(client, account, "give me my spending summary")
        other = await ask(client, account, "open expenses")

    assert run["skill"] == "explain-spending"
    assert run["message_code"].startswith("explain-spending.month.")
    assert other["skill"] != "explain-spending"


async def test_insights_are_the_users_own(agent_client: AgentClient) -> None:
    async with agent_client(mode="static") as client:
        await seeded(client)
        stranger = await seeded(client)
        await add(client, stranger, title="Gift", amount_minor=100, date="2026-09-03")
        found = await insight(client, stranger)

    assert found["facts"]["spent_minor"] == 463100
