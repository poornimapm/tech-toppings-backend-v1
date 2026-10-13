"""The free-provider chain: order, cooldown on 429, and moving on after failures."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.core.config import AgentSettings
from app.platform.agents.providers import ProviderChain, ProvidersUnavailableError
from tests.helpers import FrozenClock
from tests.platform.agents.conftest import BACKUP, BACKUP_URL, LLM_URL, PRIMARY, calls, reply

MESSAGES = [{"role": "user", "content": "hi"}]


@pytest.fixture
async def chain(clock: FrozenClock) -> ProviderChain:
    settings = AgentSettings(
        _env_file=None, providers=(PRIMARY, BACKUP), provider_cooldown_seconds=60
    )
    return ProviderChain(settings, httpx.AsyncClient(), clock)


@respx.mock
async def test_uses_the_first_provider_with_its_key(chain: ProviderChain) -> None:
    route = respx.post(LLM_URL).mock(return_value=reply("hello"))

    completion = await chain.complete(MESSAGES, [])

    assert completion.content == "hello"
    assert completion.provider == "primary"
    assert (completion.prompt_tokens, completion.completion_tokens) == (50, 10)
    assert route.calls[0].request.headers["Authorization"] == "Bearer test-key"


@respx.mock
@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(500),
        httpx.Response(200, json={"unexpected": True}),
        httpx.ConnectTimeout("slow"),
    ],
)
async def test_moves_on_after_a_failure(
    chain: ProviderChain, failure: httpx.Response | Exception
) -> None:
    respx.post(LLM_URL).mock(side_effect=[failure])
    respx.post(BACKUP_URL).mock(return_value=reply("from backup"))

    assert (await chain.complete(MESSAGES, [])).provider == "backup"


@respx.mock
async def test_a_429_rests_the_provider_until_the_cooldown_ends(
    chain: ProviderChain, clock: FrozenClock
) -> None:
    primary = respx.post(LLM_URL).mock(side_effect=[httpx.Response(429), reply("back")])
    respx.post(BACKUP_URL).mock(return_value=reply("backup"))

    await chain.complete(MESSAGES, [])
    await chain.complete(MESSAGES, [])  # primary is resting: not even asked
    clock.advance(seconds=61)
    again = await chain.complete(MESSAGES, [])

    assert primary.call_count == 2
    assert again.provider == "primary"


@respx.mock
async def test_raises_when_every_provider_fails(chain: ProviderChain) -> None:
    respx.post(LLM_URL).mock(return_value=httpx.Response(503))
    respx.post(BACKUP_URL).mock(return_value=httpx.Response(401))

    with pytest.raises(ProvidersUnavailableError):
        await chain.complete(MESSAGES, [])


@respx.mock
async def test_parses_tool_calls_and_flags_broken_arguments(chain: ProviderChain) -> None:
    respx.post(LLM_URL).mock(return_value=calls(("a_tool", {"x": 1}), ("b_tool", "{not json")))

    completion = await chain.complete(MESSAGES, [{"type": "function"}])

    assert [(c.name, c.arguments) for c in completion.tool_calls] == [
        ("a_tool", {"x": 1}),
        ("b_tool", None),
    ]
