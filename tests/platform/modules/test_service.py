"""Service rules that don't need HTTP: arrangement, tile-stat failures, settings coercion."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from beanie import PydanticObjectId
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import ModulesSettings
from app.core.repository import Scope
from app.platform.modules.discovery import LoadedModule
from app.platform.modules.manifest import ModuleManifest, ModuleStatus, TileStat
from app.platform.modules.registry import ModuleRegistry
from app.platform.modules.service import (
    EffectiveModule,
    ModuleService,
    _arrange,
    _coerce_settings,
)
from tests.helpers import FrozenClock, build_settings


def loaded(key: str, order: int = 1, tile_stat: Any = None) -> LoadedModule:
    manifest = ModuleManifest(key=key, version="1.0.0", status=ModuleStatus.LIVE, order=order)
    return LoadedModule(f"tests.{key}", manifest, None, None, (), tile_stat)


def effective(key: str, rank: int, position: int | None = None) -> EffectiveModule:
    return EffectiveModule(
        module=loaded(key, rank),
        status=ModuleStatus.LIVE,
        global_enabled=True,
        enabled=True,
        pinned=False,
        notify_requested=False,
        default_rank=rank,
        custom_position=position,
    )


def service(*modules: LoadedModule, timeout: float = 2.0) -> ModuleService:
    return ModuleService(
        ModuleRegistry(modules),
        Scope.for_user(PydanticObjectId()),
        FrozenClock(),
        build_settings(
            "mongodb://localhost/unused",
            modules=ModulesSettings(_env_file=None, tile_stat_timeout_seconds=timeout),
        ),
    )


def test_modules_added_after_a_custom_arrangement_go_last() -> None:
    arranged = _arrange([
        effective("brand_new", rank=0),
        effective("second", rank=5, position=1),
        effective("first", rank=9, position=0),
        effective("also_new", rank=1),
    ])  # fmt: skip

    assert [item.key for item in arranged] == ["first", "second", "brand_new", "also_new"]


@pytest.fixture
def warnings(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Warnings logged by the guarded runner (tile stats and widgets share it)."""
    seen: list[dict[str, Any]] = []

    class Recorder:
        def warning(self, event: str, **fields: Any) -> None:
            seen.append({"event": event, **fields})

    monkeypatch.setattr("app.core.concurrency.logger", Recorder())
    return seen


async def test_a_failing_tile_stat_is_logged_and_skipped(warnings: list[dict[str, Any]]) -> None:
    async def broken(_scope: Scope, _clock: Any) -> TileStat:
        raise RuntimeError("database exploded")

    module = loaded("demo", tile_stat=broken)
    stat = await service(module)._stat(effective_for(module))

    assert stat is None
    assert warnings == [
        {"event": "tile_stat_failed", "error_type": "RuntimeError", "module": "demo"}
    ]
    assert "exploded" not in str(warnings)  # the message may hold data: only the type is logged


async def test_a_slow_tile_stat_times_out(warnings: list[dict[str, Any]]) -> None:
    async def slow(_scope: Scope, _clock: Any) -> TileStat:
        await asyncio.sleep(5)
        return TileStat(value=1, label="never")

    module = loaded("demo", tile_stat=slow)
    stat = await service(module, timeout=0.01)._stat(effective_for(module))

    assert stat is None
    assert warnings[0]["error_type"] == "TimeoutError"


def effective_for(module: LoadedModule) -> EffectiveModule:
    return replace(effective(module.manifest.key, rank=1), module=module)


class Prefs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    theme: str = Field(default="calm", pattern="^(calm|bold)$")
    count: int = Field(default=3, ge=1, le=10)


def test_coerce_keeps_valid_values_and_drops_the_rest() -> None:
    assert _coerce_settings(Prefs, {"theme": "bold", "count": 99, "old": True}).model_dump() == {
        "theme": "bold",
        "count": 3,
    }


def test_coerce_with_nothing_stored_gives_defaults() -> None:
    assert _coerce_settings(Prefs, {}).model_dump() == {"theme": "calm", "count": 3}
