from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

from beanie import Document
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.core.config import AgentMode
from app.core.documents import OwnedDocument
from app.platform.agents.policy import Blocker
from app.platform.agents.tools import ClientAction, Proposal


class RunStatus(StrEnum):
    COMPLETED = "completed"
    UNAVAILABLE = "unavailable"  # nothing ran; ``reason`` says why (honest answer)
    INCOMPLETE = "incomplete"  # stopped early: step limit or quota reached mid-run


class RunStep(BaseModel):
    tool: str
    ok: bool
    error: str | None = None


class AgentRun(OwnedDocument):
    """One request to an agent. Kept for ``AGENT_RUN_RETENTION_DAYS`` (TTL), then deleted."""

    MODULE_KEY: ClassVar[str] = "platform"

    agent: str
    skill: str | None = None
    status: RunStatus
    mode: AgentMode | None = None
    reason: Blocker | None = None
    input: str  # already redacted
    photos: int = 0  # how many photos the run read; the photos themselves are never kept
    locale: str
    message: str | None = None  # agent mode: the agent's reply
    message_code: str | None = None  # static mode: translated on the client
    message_params: dict[str, Any] = Field(default_factory=dict)
    proposals: list[Proposal] = Field(default_factory=list)
    client_actions: list[ClientAction] = Field(default_factory=list)
    steps: list[RunStep] = Field(default_factory=list)
    provider: str | None = None
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    purge_at: datetime

    class Settings:
        name = "agent_runs"
        indexes = [  # noqa: RUF012
            IndexModel([("user_id", ASCENDING), ("created_at", DESCENDING)], name="user_recent"),
            IndexModel([("proposals.id", ASCENDING)], name="proposal_id"),
            IndexModel([("purge_at", ASCENDING)], expireAfterSeconds=0, name="purge_at_ttl"),
        ]


class AgentUsage(Document):
    """Daily AI call counters: one per user per day, plus one global per day."""

    key: str
    calls: int = 0
    purge_at: datetime

    class Settings:
        name = "agent_usage"
        indexes = [  # noqa: RUF012
            IndexModel([("key", ASCENDING)], unique=True, name="key_unique"),
            IndexModel([("purge_at", ASCENDING)], expireAfterSeconds=0, name="purge_at_ttl"),
        ]
