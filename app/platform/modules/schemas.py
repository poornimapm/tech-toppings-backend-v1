from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.platform.modules.manifest import ModuleStatus, TileStat


class ModuleView(BaseModel):
    """A module as the signed-in user sees it: manifest defaults + admin overrides + own
    preferences, already merged."""

    key: str
    slug: str
    version: str
    status: ModuleStatus
    global_enabled: bool
    enabled: bool
    available: bool  # can be opened: enabled everywhere, for this user, and not coming soon
    pinned: bool
    position: int
    notify_requested: bool
    has_settings: bool
    stat: TileStat | None


class PreferencesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    pinned: bool | None = None


class OrderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keys: list[str] = Field(min_length=1, max_length=100, description="Module keys, first to last")

    @field_validator("keys")
    @classmethod
    def no_duplicates(cls, keys: list[str]) -> list[str]:
        if len(set(keys)) != len(keys):
            raise ValueError("each module may appear only once")
        return keys
