"""The AGENT_MODE / AGENT_FALLBACK switches, every combination."""

from __future__ import annotations

import pytest

from app.core.config import AgentMode, AgentSettings
from app.platform.agents.policy import Blocker, Plan, plan

AGENT, STATIC = AgentMode.AGENT, AgentMode.STATIC


@pytest.mark.parametrize(
    ("configured", "blocker", "fallback", "has_static", "expected"),
    [
        (AGENT, None, False, False, Plan(AGENT, None)),
        (AGENT, None, True, True, Plan(AGENT, None)),
        (AGENT, Blocker.QUOTA, True, True, Plan(STATIC, Blocker.QUOTA)),
        (AGENT, Blocker.QUOTA, False, True, Plan(None, Blocker.QUOTA)),
        (AGENT, Blocker.CONSENT, True, False, Plan(None, Blocker.CONSENT)),
        (AGENT, Blocker.PROVIDERS_FAILED, True, True, Plan(STATIC, Blocker.PROVIDERS_FAILED)),
        (STATIC, None, False, True, Plan(STATIC, None)),
        (STATIC, Blocker.CONSENT, False, True, Plan(STATIC, None)),  # static never needs AI
        (STATIC, None, True, False, Plan(None, Blocker.NO_STATIC)),
    ],
)
def test_plan(
    configured: AgentMode, blocker: Blocker | None, fallback: bool, has_static: bool, expected: Plan
) -> None:
    assert plan(configured, blocker, fallback=fallback, has_static=has_static) == expected


def test_per_skill_modes_override_the_global_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_MODE", "agent")
    monkeypatch.setenv("AGENT_SKILL_MODES", "manage-modules=static")
    settings = AgentSettings(_env_file=None)

    assert settings.mode_for("manage-modules") is STATIC
    assert settings.mode_for("other") is AGENT


@pytest.mark.parametrize(
    ("variable", "value", "field"),
    [
        ("AGENT_SKILL_MODES", "manage-modules=sometimes", "skill_modes"),
        ("AGENT_REDACT_PATTERNS", '["(unclosed"]', "redact_patterns"),
        ("AGENT_PROVIDERS", '[{"name": "x"}]', "providers"),
    ],
)
def test_bad_switch_values_fail_at_startup(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str, field: str
) -> None:
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValueError, match=field):
        AgentSettings(_env_file=None)
