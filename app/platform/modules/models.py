from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from pydantic import Field
from pymongo import ASCENDING, IndexModel

from app.core.documents import OwnedDocument, TimestampedDocument
from app.platform.modules.manifest import ModuleStatus


class ModuleOverride(TimestampedDocument):
    """Admin changes to a module's manifest defaults (written by the admin console in Phase 10).
    Unset fields fall back to the manifest, so new deployments pick up manifest changes."""

    key: str
    status: ModuleStatus | None = None
    global_enabled: bool | None = None
    order: int | None = None

    class Settings:
        name = "modules"
        indexes = [IndexModel([("key", ASCENDING)], unique=True, name="key_unique")]  # noqa: RUF012


class UserModule(OwnedDocument):
    """One user's preferences for one module. Here ``module_key`` names the module the
    preferences are *for* (see UserModuleRepository.module_key_for)."""

    MODULE_KEY: ClassVar[str] = "platform"

    enabled: bool | None = None  # None = the manifest's default_enabled
    pinned: bool = False
    position: int | None = None  # None = the manifest / admin order
    settings: dict[str, Any] = Field(default_factory=dict)
    notify_requested_at: datetime | None = None

    class Settings:
        name = "user_modules"
        indexes = [  # noqa: RUF012
            IndexModel(
                [("user_id", ASCENDING), ("module_key", ASCENDING)],
                unique=True,
                name="user_module_unique",
            )
        ]
