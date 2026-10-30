from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import AgentMode, Locale
from app.platform.agents.models import AgentRun, RunStatus
from app.platform.agents.policy import Blocker
from app.platform.agents.tools import ClientAction, Proposal


class ModuleName(BaseModel):
    """A module as the user sees it on screen, so agents and static scripts match their words."""

    key: str = Field(max_length=40)
    name: str = Field(max_length=80)


class RunContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    modules: list[ModuleName] = Field(default_factory=list, max_length=100)
    input_type: Literal["text", "speech", "sms", "ocr"] | None = Field(
        default=None,
        description="How the input was given, when a page asks on purpose; ocr: text read from "
        "a photo on the device (limit: AGENT_MAX_OCR_CHARS, for a skill that reads it)",
    )


class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: str = Field(
        min_length=1, description="What the user asked (limit: AGENT_MAX_INPUT_CHARS)"
    )
    skill: str | None = Field(default=None, description="Use only this skill")
    locale: Locale | None = Field(default=None, description="Defaults to the user's language")
    context: RunContext = Field(default_factory=RunContext)


class RunOut(BaseModel):
    id: str
    agent: str
    skill: str | None
    status: RunStatus
    mode: AgentMode | None
    reason: Blocker | None
    message: str | None
    message_code: str | None
    message_params: dict[str, Any]
    proposals: list[Proposal]
    client_actions: list[ClientAction]
    created_at: datetime

    @classmethod
    def of(cls, run: AgentRun) -> RunOut:
        return cls.model_validate({**run.model_dump(), "id": str(run.id)})


class SkillView(BaseModel):
    name: str
    description: str
    mode: AgentMode
    has_static: bool
    photo_mode: AgentMode | None = Field(
        default=None,
        description="For a skill that reads photos: what a photo run would use right now (agent: "
        "the photo goes to AI; static: read it on the device); null: it takes no photos or "
        "can't run",
    )


class AgentView(BaseModel):
    name: str
    description: str
    mode: AgentMode | None = Field(description="What a run would use right now; null = unavailable")
    reason: Blocker | None
    fallback: bool
    quota_remaining: int
    skills: list[SkillView]
