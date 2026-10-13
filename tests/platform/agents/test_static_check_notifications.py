"""Static mode of check-notifications, and how skills share one request in static mode."""

from __future__ import annotations

from typing import Any

import pytest
from beanie import PydanticObjectId

from app.core.repository import Scope
from app.platform.agents.lexicon import load_lexicon, match_action, mentions, normalise
from app.platform.notifications.inbox import Inbox
from tests.helpers import Account, FrozenClock, build_settings, register
from tests.platform.agents.conftest import AgentClient, ask


async def run_with_unread(
    agent_client: AgentClient, text: str, unread: int
) -> tuple[dict[str, Any], Account, Any]:
    async with agent_client(mode="static") as client:
        account = await register(client)
        inbox = Inbox(
            Scope.for_user(PydanticObjectId(account.user_id)),
            FrozenClock(),
            build_settings("mongodb://localhost/unused"),
        )
        for index in range(unread):
            await inbox.notify("module.launched", module=f"module{index}")
        run = await ask(client, account, text)
        if run["proposals"]:
            proposal = run["proposals"][0]["id"]
            await client.post(f"/v1/agents/proposals/{proposal}/confirm", headers=account.headers)
        left = (await client.get("/v1/notifications?unread=true", headers=account.headers)).json()
    return run, account, left["total"]


@pytest.mark.parametrize("text", ["what's new?", "show my notifications", "मेरी सूचनाएँ दिखाओ"])
async def test_shows_what_is_waiting(agent_client: AgentClient, text: str) -> None:
    run, _, left = await run_with_unread(agent_client, text, unread=2)

    assert run["skill"] == "check-notifications"  # not hijacked by "show <module>"
    assert (run["message_code"], run["message_params"]) == (
        "check-notifications.ready.show",
        {"count": 2},
    )
    assert run["client_actions"] == [{"tool": "notifications_open", "arguments": {}}]
    assert left == 2  # looking is not reading


async def test_marking_everything_read_is_a_confirmed_change(agent_client: AgentClient) -> None:
    run, _, left = await run_with_unread(agent_client, "mark all as read", unread=3)

    assert run["proposals"][0]["tool"] == "notifications_mark_all_read"
    assert run["proposals"][0]["summary_code"] == "check-notifications.change.mark-read"
    assert left == 0  # confirmed by the helper


async def test_nothing_unread_is_said_not_proposed(agent_client: AgentClient) -> None:
    run, _, _ = await run_with_unread(agent_client, "clear notifications", unread=0)

    assert (run["message_code"], run["proposals"]) == ("check-notifications.none-unread", [])


async def test_a_tentative_answer_is_used_when_no_skill_is_sure(agent_client: AgentClient) -> None:
    run, _, _ = await run_with_unread(agent_client, "pin the spaceship", unread=0)

    assert (run["skill"], run["message_code"]) == ("manage-modules", "manage-modules.no-module")


def test_lexicon_matching_rules(tmp_path: Any) -> None:
    (tmp_path / "lexicon.yaml").write_text(
        "actions:\n  specific: {words: ['mark all']}\n  general: {words: [pin, 'பின்']}\n",
        encoding="utf-8",
    )
    lexicon = load_lexicon(tmp_path)

    assert normalise("  PIN   \uff29t ") == "pin it"  # case, spacing, full-width I
    assert mentions("please pin it", "pin")
    assert not mentions("spinach", "pin")  # English matches whole words
    assert mentions("பின்செய்", "பின்")  # Tamil stems match inside inflected words
    assert match_action("mark all and pin", lexicon) == ("specific", {"words": ["mark all"]})
    assert match_action("nothing here", lexicon) is None
