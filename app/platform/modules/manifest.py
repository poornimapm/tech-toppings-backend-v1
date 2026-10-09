"""What a feature module declares so the platform can mount it (ADR-0001).

A module is a folder under a configured package (``MODULES_PACKAGES``) with a ``manifest.py``
defining ``MANIFEST = ModuleManifest(...)``. Presentation (name, icon, accent colour) lives in
the module's frontend manifest; this one describes behaviour only.

References to code are import paths (``"app.modules.expenses.router:router"``) resolved at
startup, so a manifest stays cheap to import and the discovery step can check that every
path points inside the module's own package.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.clock import Clock
from app.core.repository import Scope

IMPORT_PATH = r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$"
SEMVER = r"^\d+\.\d+\.\d+$"


class ModuleStatus(StrEnum):
    LIVE = "live"
    BETA = "beta"
    COMING_SOON = "coming_soon"


class TileStat(BaseModel):
    """A small figure for the module's Welcome tile (e.g. ``₹4,320 this month``).

    ``label`` is a key in the module's frontend strings, so the client translates it."""

    value: float
    format: Literal["number", "currency"] = "number"
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    label: str = Field(min_length=1, max_length=64)


TileStatProvider = Callable[[Scope, Clock], Awaitable[TileStat | None]]


class ModuleManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,30}$", description="Equals the folder name")
    version: str = Field(pattern=SEMVER)
    status: ModuleStatus
    order: int = Field(ge=0, description="Default position on the Welcome page")
    default_enabled: bool = True
    settings_model: type[BaseModel] | None = None
    router: str | None = Field(default=None, pattern=IMPORT_PATH)
    admin_router: str | None = Field(default=None, pattern=IMPORT_PATH)
    documents: tuple[str, ...] = ()
    tile_stat: str | None = Field(default=None, pattern=IMPORT_PATH)

    @property
    def slug(self) -> str:
        """URL segment: ``/v1/<slug>``, ``/v1/admin/<slug>``."""
        return self.key.replace("_", "-")

    @model_validator(mode="after")
    def upcoming_modules_have_no_endpoints(self) -> ModuleManifest:
        if self.status is ModuleStatus.COMING_SOON and (self.router or self.admin_router):
            raise ValueError("a coming-soon module cannot expose routes yet")
        return self

    def code_paths(self) -> list[str]:
        """Every import path this manifest references."""
        paths = [self.router, self.admin_router, self.tile_stat, *self.documents]
        return [path for path in paths if path]
