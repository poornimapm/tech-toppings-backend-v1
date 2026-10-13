"""Which mode a run uses: the ``AGENT_MODE`` / ``AGENT_FALLBACK`` switches applied to what is
blocking AI right now. A pure function, so every combination is easy to test."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.core.config import AgentMode


class Blocker(StrEnum):
    CONSENT = "consent"  # the user hasn't turned AI on
    QUOTA = "quota"  # daily AI calls used up (user or global)
    NO_PROVIDER = "no_provider"  # no provider configured
    PROVIDERS_FAILED = "providers_failed"  # every provider failed or is resting
    NO_STATIC = "no_static"  # static mode requested but the skill has no static script


@dataclass(frozen=True)
class Plan:
    mode: AgentMode | None  # None: don't run, answer honestly that it's unavailable
    reason: Blocker | None  # why AI isn't used (also set when static ran as a fallback)


def plan(
    configured: AgentMode, blocker: Blocker | None, *, fallback: bool, has_static: bool
) -> Plan:
    if configured is AgentMode.STATIC:
        return Plan(AgentMode.STATIC, None) if has_static else Plan(None, Blocker.NO_STATIC)
    if blocker is None:
        return Plan(AgentMode.AGENT, None)
    if fallback and has_static:
        return Plan(AgentMode.STATIC, blocker)
    return Plan(None, blocker)
