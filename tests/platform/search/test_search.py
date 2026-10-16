"""``GET /v1/search``: every module the user uses finds their own things at once. A module
switched off is not asked, and a slow or broken one is left out of the answer."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from app.core.config import SearchSettings
from app.platform.search.contract import SearchHit, search
from tests.helpers import Account, build_app, build_settings, register, running_client

SEARCH = "/v1/search"


async def note(client: AsyncClient, account: Account, text: str) -> str:
    response = await client.post("/v1/notes", headers=account.headers, json={"text": text})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def found(client: AsyncClient, account: Account, q: str) -> dict[str, Any]:
    response = await client.get(SEARCH, headers=account.headers, params={"q": q})
    assert response.status_code == 200, response.text
    return dict(response.json())


def owners(answer: dict[str, Any]) -> list[str]:
    return [group["owner"] for group in answer["groups"]]


async def test_finds_the_users_own_things_by_module(client: AsyncClient) -> None:
    account = await register(client)
    note_id = await note(client, account, "Call the plumber")
    other = await register(client)
    await note(client, other, "plumber for them")

    answer = await found(client, account, "  PLUMBER ")
    assert answer["query"] == "PLUMBER"
    assert answer["groups"] == [
        {
            "owner": "notes",
            "hits": [
                {
                    "kind": "note",
                    "id": note_id,
                    "title": "Call the plumber",
                    "path": f"notes/{note_id}",
                    "amount_minor": None,
                    "currency": None,
                    "date": None,
                }
            ],
        }
    ]


async def test_a_query_too_short_finds_nothing(client: AsyncClient) -> None:
    account = await register(client)
    await note(client, account, "a")
    assert await found(client, account, " a ") == {"query": "a", "groups": []}


async def test_each_module_answers_with_at_most_the_configured_hits(client: AsyncClient) -> None:
    account = await register(client)
    for n in range(12):
        await note(client, account, f"Milk {n}")
    answer = await found(client, account, "milk")
    assert len(answer["groups"][0]["hits"]) == SearchSettings(_env_file=None).max_hits


async def test_a_module_switched_off_is_not_asked(client: AsyncClient) -> None:
    account = await register(client)
    await note(client, account, "plumber")
    await client.patch(
        "/v1/modules/notes/preferences", headers=account.headers, json={"enabled": False}
    )
    assert "notes" not in owners(await found(client, account, "plumber"))


@pytest.mark.parametrize("word", ["slow", "boom", "away"])
async def test_a_slow_or_broken_module_is_left_out(word: str, own_mongo_uri: str) -> None:
    settings = build_settings(
        own_mongo_uri, search=SearchSettings(_env_file=None, timeout_seconds=0.2)
    )
    async with running_client(build_app(settings)) as client:
        account = await register(client)
        await note(client, account, f"{word} plumber")
        answer = await found(client, account, f"{word} plumber")
    assert answer["groups"] == []


async def test_needs_a_signed_in_user(client: AsyncClient) -> None:
    assert (await client.get(SEARCH, params={"q": "plumber"})).status_code == 401


def test_a_provider_must_be_async() -> None:
    def not_async(*_: Any) -> list[SearchHit]:
        return []

    with pytest.raises(TypeError, match="must be async"):
        search(not_async)  # type: ignore[type-var]
