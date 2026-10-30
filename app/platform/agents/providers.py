"""Free LLMs through one protocol: OpenAI-compatible chat completions with tool calling
(Gemini's OpenAI endpoint, Groq, OpenRouter free models, a local Ollama ...).

Providers are tried in configured order. A 429 rests that provider for the cooldown; errors,
timeouts and malformed answers (a tool call cut off at the length limit among them) move on
to the next one. Photos go only to providers that read images (``vision``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx

from app.core.clock import Clock
from app.core.config import AgentSettings, ProviderSettings
from app.core.logging import get_logger

logger = get_logger(__name__)


class ProvidersUnavailableError(Exception):
    pass


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] | None  # None: the model sent arguments that aren't JSON


@dataclass(frozen=True)
class Completion:
    message: dict[str, Any]  # the assistant message, to append to the conversation
    content: str | None
    tool_calls: tuple[ToolCall, ...]
    provider: str
    prompt_tokens: int
    completion_tokens: int
    cut: bool = False  # stopped at the length limit

    @property
    def broken(self) -> bool:
        """Cut off inside a tool call: asking again with the same room gives the same."""
        return self.cut and any(call.arguments is None for call in self.tool_calls)


def _arguments(raw: Any) -> dict[str, Any] | None:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _parse(body: dict[str, Any], provider: str) -> Completion:
    message = body["choices"][0]["message"]
    calls = tuple(
        ToolCall(
            call["id"], call["function"]["name"], _arguments(call["function"].get("arguments"))
        )
        for call in message.get("tool_calls") or []
    )
    usage = body.get("usage") or {}
    return Completion(
        message={key: message[key] for key in ("role", "content", "tool_calls") if key in message},
        content=message.get("content"),
        tool_calls=calls,
        provider=provider,
        prompt_tokens=int(usage.get("prompt_tokens", 0)),
        completion_tokens=int(usage.get("completion_tokens", 0)),
        cut=body["choices"][0].get("finish_reason") == "length",
    )


class ProviderChain:
    def __init__(self, settings: AgentSettings, http: httpx.AsyncClient, clock: Clock) -> None:
        self._providers = settings.providers
        self._cooldown = timedelta(seconds=settings.provider_cooldown_seconds)
        self._http = http
        self._clock = clock
        self._resting_until: dict[str, datetime] = {}

    def configured(self, *, vision: bool = False) -> bool:
        """Is there a provider for this run (one that reads images, for a run with photos)?"""
        return any(provider.vision or not vision for provider in self._providers)

    def ready(self, *, vision: bool = False) -> bool:
        """Is one of them not resting after a 429? (Else a run would spend quota for nothing.)"""
        now = self._clock.now()
        return any(
            (provider.vision or not vision) and self._resting_until.get(provider.name, now) <= now
            for provider in self._providers
        )

    async def _call(
        self, provider: ProviderSettings, payload: dict[str, Any], max_tokens: int | None
    ) -> Completion | None:
        headers = (
            {"Authorization": f"Bearer {provider.api_key.get_secret_value()}"}
            if provider.api_key
            else {}
        )
        try:
            response = await self._http.post(
                f"{provider.base_url.rstrip('/')}/chat/completions",
                json={
                    **payload,
                    "model": provider.model,
                    "temperature": provider.temperature,
                    "max_tokens": max(provider.max_tokens, max_tokens or 0),
                },
                headers=headers,
                timeout=provider.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            logger.warning("agent_provider_error", provider=provider.name, error=type(exc).__name__)
            return None
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            self._resting_until[provider.name] = self._clock.now() + self._cooldown
        if response.is_error:
            logger.warning(
                "agent_provider_refused", provider=provider.name, status=response.status_code
            )
            return None
        try:
            completion = _parse(response.json(), provider.name)
        except (KeyError, IndexError, TypeError, ValueError):
            logger.warning("agent_provider_bad_answer", provider=provider.name)
            return None
        if completion.broken:
            logger.warning("agent_provider_cut_off", provider=provider.name)
            return None
        return completion

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        vision: bool = False,
        max_tokens: int | None = None,
    ) -> Completion:
        """``vision``: the messages carry photos, so only providers that read images are asked.
        ``max_tokens``: room the answer needs, when more than a provider's own setting."""
        payload: dict[str, Any] = {"messages": messages}
        if tools:
            payload |= {"tools": tools, "tool_choice": "auto"}
        now = self._clock.now()
        for provider in self._providers:
            if self._resting_until.get(provider.name, now) > now or (
                vision and not provider.vision
            ):
                continue
            completion = await self._call(provider, payload, max_tokens)
            if completion is not None:
                return completion
        raise ProvidersUnavailableError
