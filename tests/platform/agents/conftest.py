"""Agent test helpers: an app with chosen agent settings, consent, and fake LLM replies in the
OpenAI chat-completions format (respx), so no test ever calls a real provider."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest
from httpx import AsyncClient, Response
from pydantic import SecretStr

from app.core.config import AgentSettings, ProviderSettings
from tests.helpers import Account, FrozenClock, build_app, build_settings, running_client

LLM_URL = "https://llm.test/v1/chat/completions"
BACKUP_URL = "https://backup.test/v1/chat/completions"
PRIMARY = ProviderSettings(
    name="primary",
    base_url="https://llm.test/v1",
    model="free-model",
    api_key=SecretStr("test-key"),
)
BACKUP = ProviderSettings(name="backup", base_url="https://backup.test/v1", model="free-model")

AgentClient = Callable[..., AbstractAsyncContextManager[AsyncClient]]


@pytest.fixture
def agent_client(mongo_uri: str, clock: FrozenClock) -> AgentClient:
    """``async with agent_client(mode="static", ...) as client:`` with those AGENT_ settings."""

    def open_client(**agent: Any) -> AbstractAsyncContextManager[AsyncClient]:
        settings = build_settings(mongo_uri, agent=AgentSettings(_env_file=None, **agent))
        return running_client(build_app(settings, clock))

    return open_client


@pytest.fixture
async def ai_client(agent_client: AgentClient) -> AsyncIterator[AsyncClient]:
    """Agent mode with one provider and no fallback."""
    async with agent_client(providers=[PRIMARY], fallback=False) as client:
        yield client


async def give_consent(client: AsyncClient, account: Account) -> None:
    response = await client.patch("/v1/me", headers=account.headers, json={"ai_consent": True})
    assert response.status_code == 200


async def ask(client: AsyncClient, account: Account, text: str, **extra: Any) -> dict[str, Any]:
    response = await client.post(
        "/v1/agents/assistant/runs", headers=account.headers, json={"input": text, **extra}
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def reply(text: str) -> Response:
    return Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10},
        },
    )


def calls(*tool_calls: tuple[str, dict[str, Any] | str]) -> Response:
    """A reply that calls tools; a str argument is sent as-is (to test broken JSON)."""
    return Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": f"call-{i}",
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": args
                                    if isinstance(args, str)
                                    else json.dumps(args),
                                },
                            }
                            for i, (name, args) in enumerate(tool_calls)
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 80, "completion_tokens": 20},
        },
    )


def sent(route: Any, index: int = -1) -> dict[str, Any]:
    """The JSON body of a recorded provider request."""
    return dict(json.loads(route.calls[index].request.content))
