"""A module's skills and tools serve a user only while the module is available to them.

Uses the test module (tests/fixtures/modules/notes): its ``take-notes`` skill proposes a note.
"""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient

from tests.helpers import Account, register
from tests.platform.agents.conftest import AgentClient, ask


async def notes(client: AsyncClient, account: Account) -> list[str]:
    page = (await client.get("/v1/notes", headers=account.headers)).json()
    return [note["text"] for note in page["items"]]


async def switch_notes(client: AsyncClient, account: Account, *, on: bool) -> None:
    response = await client.patch(
        "/v1/modules/notes/preferences", headers=account.headers, json={"enabled": on}
    )
    assert response.status_code == 200


async def confirm(client: AsyncClient, account: Account, run: dict[str, Any]) -> dict[str, Any]:
    proposal = run["proposals"][0]["id"]
    response = await client.post(
        f"/v1/agents/proposals/{proposal}/confirm", headers=account.headers
    )
    return dict(response.json())


async def test_a_modules_skill_proposes_and_the_user_confirms(agent_client: AgentClient) -> None:
    async with agent_client(mode="static") as client:
        account = await register(client)
        run = await ask(client, account, "jot buy milk")
        before = await notes(client, account)
        confirmed = await confirm(client, account, run)
        after = await notes(client, account)

    assert (run["skill"], run["proposals"][0]["tool"]) == ("take-notes", "notes_add")
    assert (before, confirmed["status"], after) == ([], "confirmed", ["buy milk"])


async def test_a_switched_off_module_brings_no_skill(agent_client: AgentClient) -> None:
    async with agent_client(mode="static") as client:
        account = await register(client)
        await switch_notes(client, account, on=False)
        run = await ask(client, account, "jot buy milk")

    assert (run["skill"], run["proposals"], run["message_code"]) == (None, [], "static.no_match")


async def test_a_proposal_cannot_be_confirmed_once_its_module_is_off(
    agent_client: AgentClient,
) -> None:
    async with agent_client(mode="static") as client:
        account = await register(client)
        run = await ask(client, account, "jot buy milk")
        await switch_notes(client, account, on=False)
        confirmed = await confirm(client, account, run)
        await switch_notes(client, account, on=True)
        saved = await notes(client, account)

    assert (confirmed["status"], confirmed["error_code"]) == ("failed", "MODULE_DISABLED")
    assert saved == []
