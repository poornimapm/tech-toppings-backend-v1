"""Finding products the user follows under two names (the tidy-products skill): suggestions to
confirm, never a change on its own. Static mode is the word groups and spelling; agent mode a
(simulated) AI provider using the same tools."""

from __future__ import annotations

import json
from typing import Any

import respx
from httpx import AsyncClient

from tests.helpers import Account, register
from tests.modules.expenses.conftest import add
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

SKILL = "tidy-products"
PRODUCTS = "/v1/expenses/products"
FIND = "Find products that are the same thing"


async def shop(client: AsyncClient, account: Account) -> dict[str, str]:
    """Tomato twice and Thakkali once (both followed), "Tomatoes" without a quantity (not
    followed), and two different things: Tomato sauce and Rice. Their product ids by name."""
    for title, quantity, unit in (
        ("Tomato", 1, "kg"),
        ("Tomato", 2, "kg"),
        ("Thakkali", 1, "kg"),
        ("Tomato sauce", 1, "bottle"),
        ("Rice", 5, "kg"),
    ):
        await add(client, account, title=title, quantity=quantity, unit=unit)
    await add(client, account, title="Tomatoes")
    listed = await client.get(PRODUCTS, headers=account.headers)
    return {item["name"]: item["id"] for item in listed.json()["items"]}


def matches_of(run: dict[str, Any]) -> list[dict[str, Any]]:
    (action,) = run["client_actions"]
    assert action["tool"] == "expenses_product_matches"
    return list(action["arguments"]["matches"])


async def test_the_same_thing_under_other_names_is_suggested_not_combined(
    agent_client: AgentClient,
) -> None:
    async with agent_client(mode="static") as client:
        account = await register(client)
        ids = await shop(client, account)
        run = await ask(client, account, FIND, skill=SKILL)
        still = await client.get(PRODUCTS, headers=account.headers)

    assert (run["skill"], run["message_code"]) == (SKILL, "tidy-products.ready.find")
    assert matches_of(run) == [
        {"product_id": ids["Tomato"], "products": [ids["Thakkali"]], "titles": ["Tomatoes"]}
    ]
    assert still.json()["total"] == 4  # nothing combined until the user says so


async def test_products_said_to_differ_are_not_suggested_again(agent_client: AgentClient) -> None:
    async with agent_client(mode="static") as client:
        account = await register(client)
        ids = await shop(client, account)
        await client.post(
            f"{PRODUCTS}/{ids['Thakkali']}/apart",
            headers=account.headers,
            json={"product_id": ids["Tomato"]},
        )
        await client.patch(
            f"{PRODUCTS}/{ids['Tomato']}",
            headers=account.headers,
            json={"aliases": ["tomato", "tomatoes"]},
        )
        run = await ask(client, account, FIND, skill=SKILL)

    assert (run["message_code"], run["client_actions"]) == ("tidy-products.none", [])


async def test_the_general_assistant_tidies_only_when_asked_to(agent_client: AgentClient) -> None:
    async with agent_client(mode="static") as client:
        account = await register(client)
        await shop(client, account)
        asked = await ask(client, account, "please find duplicate products")
        other = await ask(client, account, "how is the weather")

    assert asked["skill"] == SKILL
    assert len(matches_of(asked)) == 1
    assert other["skill"] != SKILL


@respx.mock
async def test_ai_suggests_matches_and_unknown_products_go_back_to_it(
    agent_client: AgentClient,
) -> None:
    async with agent_client(providers=[PRIMARY]) as client:
        account = await register(client)
        await give_consent(client, account)
        ids = await shop(client, account)
        made_up = {"product_id": "000000000000000000000000", "titles": ["Tomatoes"]}
        right = {"product_id": ids["Tomato"], "products": [ids["Thakkali"]]}
        route = respx.post(LLM_URL).mock(
            side_effect=[
                calls(("expenses_products", {}), ("expenses_items", {})),
                calls(("expenses_product_matches", {"matches": [made_up]})),
                calls(("expenses_product_matches", {"matches": [right]})),
                reply("Thakkali looks like Tomato."),
            ]
        )
        run = await ask(client, account, FIND, skill=SKILL)

    looked_up = [m["content"] for m in sent(route, 1)["messages"] if m["role"] == "tool"]
    known = json.loads(looked_up[0])["result"]
    assert {item["name"] for item in known} == {"Tomato", "Thakkali", "Tomato sauce", "Rice"}
    assert json.loads(looked_up[1])["result"] == [{"title": "Tomatoes", "times": 1}]
    refused = [m["content"] for m in sent(route, 2)["messages"] if m["role"] == "tool"]
    assert "error" in refused[-1]
    assert run["message"] == "Thakkali looks like Tomato."
    assert matches_of(run) == [right | {"titles": []}]
