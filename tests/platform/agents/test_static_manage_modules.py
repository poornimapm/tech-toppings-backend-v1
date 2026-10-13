"""Static mode of manage-modules: the lexicon (data), not code, understands the request."""

from __future__ import annotations

from typing import Any

import pytest

from tests.helpers import register
from tests.platform.agents.conftest import AgentClient, ask

SHOWN = {
    "en": [
        {"key": "habits", "name": "Habit tracker"},
        {"key": "receipts", "name": "Receipts & warranty"},
        {"key": "journal", "name": "Journal"},
        {"key": "study", "name": "Study helper"},
    ],
    "ta": [{"key": "habits", "name": "பழக்கக் கண்காணிப்பு"}, {"key": "journal", "name": "நாட்குறிப்பு"}],
    "hi": [{"key": "habits", "name": "आदत ट्रैकर"}, {"key": "journal", "name": "डायरी"}],
}


async def static_run(agent_client: AgentClient, text: str, language: str = "en") -> dict[str, Any]:
    async with agent_client(mode="static") as client:
        account = await register(client)
        return await ask(client, account, text, context={"modules": SHOWN[language]})


@pytest.mark.parametrize(
    ("text", "language", "tool", "arguments", "name"),
    [
        (
            "pin the habbit tracker",
            "en",
            "modules_set_preferences",
            {"pinned": True},
            "Habit tracker",
        ),
        (
            "notify me when receipts launches",
            "en",
            "modules_set_interest",
            {"wanted": True},
            "Receipts & warranty",
        ),
        ("hide journal", "en", "modules_set_preferences", {"enabled": False}, "Journal"),
        (
            "பழக்கக் கண்காணிப்பு விருப்பங்களில் சேர்",
            "ta",
            "modules_set_preferences",
            {"pinned": True},
            "பழக்கக் கண்காணிப்பு",
        ),
        ("डायरी छिपाओ", "hi", "modules_set_preferences", {"enabled": False}, "डायरी"),
    ],
)
async def test_understands_requests_in_every_language(
    agent_client: AgentClient,
    text: str,
    language: str,
    tool: str,
    arguments: dict[str, Any],
    name: str,
) -> None:
    run = await static_run(agent_client, text, language)

    (proposal,) = run["proposals"]
    assert proposal["tool"] == tool
    assert {k: v for k, v in proposal["arguments"].items() if v is not None} == {
        "key": SHOWN[language][[m["name"] for m in SHOWN[language]].index(name)]["key"],
        **arguments,
    }
    assert proposal["summary_params"] == {"name": name}
    assert run["message_params"] == {"name": name}


async def test_open_is_a_client_action(agent_client: AgentClient) -> None:
    run = await static_run(agent_client, "open journal")

    assert run["proposals"] == []
    assert run["client_actions"] == [{"tool": "modules_open", "arguments": {"key": "journal"}}]
    assert run["message_code"] == "manage-modules.ready.open"


async def test_nothing_to_do_is_said_not_proposed(agent_client: AgentClient) -> None:
    run = await static_run(agent_client, "unpin study helper")

    assert run["proposals"] == []
    assert run["message_code"] == "manage-modules.already.unpin"


async def test_an_unknown_module_is_reported(agent_client: AgentClient) -> None:
    run = await static_run(agent_client, "pin the spaceship")

    assert (run["message_code"], run["proposals"]) == ("manage-modules.no-module", [])


async def test_works_with_keys_when_the_screen_names_are_not_sent(
    agent_client: AgentClient,
) -> None:
    async with agent_client(mode="static") as client:
        account = await register(client)
        run = await ask(client, account, "pin habits")

    assert run["proposals"][0]["arguments"]["key"] == "habits"
