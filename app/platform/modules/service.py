"""What each user sees of the module catalog, and the preferences they can change.

Three layers decide a module's state, later ones winning where they say something:

1. the manifest (status, default order, enabled by default),
2. admin overrides in the ``modules`` collection (status, global switch, order),
3. the user's own ``user_modules`` record (enabled, pinned, position, settings, notify-me).

``ensure_available`` is the backend gate for module routes: hiding a tile in the client is
never what stops a user from reaching a module that is switched off.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from app.core.clock import Clock
from app.core.config import ModulesSettings
from app.core.logging import get_logger
from app.core.repository import Scope
from app.platform.modules.errors import (
    InvalidModuleSettingsError,
    ModuleDisabledError,
    ModuleHasNoSettingsError,
    ModuleNotUpcomingError,
    UnknownModuleError,
)
from app.platform.modules.manifest import ModuleStatus, TileStat
from app.platform.modules.models import ModuleOverride, UserModule
from app.platform.modules.repository import UserModuleRepository
from app.platform.modules.schemas import ModuleView, PreferencesIn

if TYPE_CHECKING:
    from app.platform.modules.discovery import LoadedModule
    from app.platform.modules.registry import ModuleRegistry

logger = get_logger(__name__)


@dataclass(frozen=True)
class EffectiveModule:
    module: LoadedModule
    status: ModuleStatus
    global_enabled: bool
    enabled: bool
    pinned: bool
    notify_requested: bool
    default_rank: int
    custom_position: int | None

    @property
    def key(self) -> str:
        return self.module.manifest.key

    @property
    def available(self) -> bool:
        return self.global_enabled and self.enabled and self.status is not ModuleStatus.COMING_SOON


def _effective(
    module: LoadedModule, override: ModuleOverride | None, preference: UserModule | None
) -> EffectiveModule:
    manifest = module.manifest
    status = override.status if override and override.status else manifest.status
    global_enabled = (
        override.global_enabled if override and override.global_enabled is not None else True
    )
    order = override.order if override and override.order is not None else manifest.order
    user_enabled = preference.enabled if preference else None
    return EffectiveModule(
        module=module,
        status=status,
        global_enabled=global_enabled,
        enabled=manifest.default_enabled if user_enabled is None else user_enabled,
        pinned=bool(preference and preference.pinned),
        notify_requested=bool(preference and preference.notify_requested_at),
        default_rank=order,
        custom_position=preference.position if preference else None,
    )


def _arrange(modules: Sequence[EffectiveModule]) -> list[EffectiveModule]:
    """The user's own arrangement first; modules they never placed (e.g. added later) follow
    in the admin/manifest order."""

    def rank(item: EffectiveModule) -> tuple[int, int, int, str]:
        placed = item.custom_position is not None
        return (0 if placed else 1, item.custom_position or 0, item.default_rank, item.key)

    return sorted(modules, key=rank)


def validation_details(error: ValidationError) -> list[dict[str, Any]]:
    """Error locations and messages without the submitted values."""
    return [
        {"loc": list(item.get("loc", ())), "msg": item.get("msg", ""), "type": item.get("type")}
        for item in error.errors()
    ]


def _coerce_settings(model: type[BaseModel], stored: Mapping[str, Any]) -> BaseModel:
    """Stored settings validated against the current model. Values a newer module version no
    longer accepts are dropped (falling back to defaults) instead of breaking the module."""
    values = dict(stored)
    for _ in range(len(values) + 1):
        try:
            return model.model_validate(values)
        except ValidationError as exc:
            invalid = {item["loc"][0] for item in exc.errors() if item.get("loc")}
            remaining = {key: value for key, value in values.items() if key not in invalid}
            if remaining == values:
                break
            values = remaining
    return model()


class ModuleService:
    def __init__(
        self, registry: ModuleRegistry, scope: Scope, clock: Clock, settings: ModulesSettings
    ) -> None:
        self.registry = registry
        self.scope = scope
        self.clock = clock
        self.settings = settings
        self.preferences = UserModuleRepository(scope, clock)

    # ------------------------------------------------------------------ reading

    async def arranged(self) -> list[EffectiveModule]:
        overrides = {item.key: item for item in await ModuleOverride.find_all().to_list()}
        preferences = await self.preferences.by_module()
        modules = [
            _effective(
                module, overrides.get(module.manifest.key), preferences.get(module.manifest.key)
            )
            for module in self.registry
        ]
        return _arrange(modules)

    async def one(self, key: str) -> EffectiveModule:
        module = self.registry.get(key)
        override = await ModuleOverride.find_one({"key": key})
        return _effective(module, override, await self.preferences.for_module(key))

    async def ensure_available(self, key: str) -> None:
        if not (await self.one(key)).available:
            raise ModuleDisabledError(details={"module": key})

    async def views(
        self, modules: Sequence[EffectiveModule], arranged: Sequence[EffectiveModule]
    ) -> list[ModuleView]:
        """Views of ``modules`` (a filtered page of ``arranged``); tile stats run in parallel."""
        positions = {item.key: index for index, item in enumerate(arranged)}
        stats = await asyncio.gather(*(self._stat(item) for item in modules))
        return [
            self._view(item, positions[item.key], stat)
            for item, stat in zip(modules, stats, strict=True)
        ]

    async def view(self, key: str) -> ModuleView:
        arranged = await self.arranged()
        index = next((i for i, item in enumerate(arranged) if item.key == key), None)
        if index is None:
            raise UnknownModuleError(details={"module": key})
        item = arranged[index]
        return self._view(item, index, await self._stat(item))

    @staticmethod
    def _view(item: EffectiveModule, position: int, stat: TileStat | None) -> ModuleView:
        manifest = item.module.manifest
        return ModuleView(
            key=manifest.key,
            slug=manifest.slug,
            version=manifest.version,
            status=item.status,
            global_enabled=item.global_enabled,
            enabled=item.enabled,
            available=item.available,
            pinned=item.pinned,
            position=position,
            notify_requested=item.notify_requested,
            has_settings=manifest.settings_model is not None,
            stat=stat,
        )

    async def _stat(self, item: EffectiveModule) -> TileStat | None:
        provider = item.module.tile_stat
        if provider is None or not item.available:
            return None
        try:
            return await asyncio.wait_for(
                provider(self.scope, self.clock), timeout=self.settings.tile_stat_timeout_seconds
            )
        except Exception as exc:  # one broken module must not break the Welcome page
            logger.warning("tile_stat_failed", module=item.key, error_type=type(exc).__name__)
            return None

    # ------------------------------------------------------------------ preferences

    async def update_preferences(self, key: str, changes: PreferencesIn) -> ModuleView:
        self.registry.get(key)
        values = changes.model_dump(exclude_unset=True, exclude_none=True)
        if values:
            await self.preferences.upsert(key, values)
        return await self.view(key)

    async def reorder(self, keys: Sequence[str]) -> None:
        """Listed modules go first, in the given order; the rest keep their relative order."""
        unknown = sorted(
            {key for key in keys if key not in {m.manifest.key for m in self.registry}}
        )
        if unknown:
            raise UnknownModuleError(details={"modules": unknown})
        current = [item.key for item in await self.arranged()]
        final = [*keys, *(key for key in current if key not in set(keys))]
        for position, key in enumerate(final):
            await self.preferences.upsert(key, {"position": position})

    async def set_interest(self, key: str, *, wanted: bool) -> ModuleView:
        item = await self.one(key)
        if wanted:
            if item.status is not ModuleStatus.COMING_SOON:
                raise ModuleNotUpcomingError(details={"module": key})
            if not item.notify_requested:  # keep the first request's time
                await self.preferences.upsert(key, {"notify_requested_at": self.clock.now()})
        elif item.notify_requested:
            await self.preferences.upsert(key, {"notify_requested_at": None})
        return await self.view(key)

    # ------------------------------------------------------------------ settings

    def _settings_model(self, key: str) -> type[BaseModel]:
        model = self.registry.get(key).manifest.settings_model
        if model is None:
            raise ModuleHasNoSettingsError(details={"module": key})
        return model

    def settings_schema(self, key: str) -> dict[str, Any]:
        return self._settings_model(key).model_json_schema()

    async def read_settings(self, key: str) -> dict[str, Any]:
        model = self._settings_model(key)
        preference = await self.preferences.for_module(key)
        stored = preference.settings if preference else {}
        return _coerce_settings(model, stored).model_dump(mode="json")

    async def save_settings(self, key: str, values: Mapping[str, Any]) -> dict[str, Any]:
        model = self._settings_model(key)
        try:
            validated = model.model_validate(dict(values))
        except ValidationError as exc:
            raise InvalidModuleSettingsError(details=validation_details(exc)) from exc
        stored = validated.model_dump(mode="json")
        await self.preferences.upsert(key, {"settings": stored})
        return stored
