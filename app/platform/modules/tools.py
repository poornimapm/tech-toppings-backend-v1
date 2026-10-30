"""Agent tools for the module catalog: thin wrappers over ModuleService, so agents follow the
same rules (and the same per-user scope) as the API."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from app.platform.agents.tools import tool
from app.platform.context import ActorContext
from app.platform.modules.schemas import ModuleView, PreferencesIn
from app.platform.modules.service import ModuleService

ModuleKey = Annotated[str, Field(description="A module key from modules_list")]


def _service(ctx: ActorContext) -> ModuleService:
    return ModuleService.of(ctx)


@tool("read")
async def modules_list(ctx: ActorContext) -> list[dict[str, Any]]:
    """List the user's modules in their order: key, status (live, beta, coming_soon), whether it
    can be opened (available), pinned (favourite), enabled (shown on their page) and whether a
    launch notice is on."""
    service = _service(ctx)
    return [
        {
            "key": item.key,
            "status": item.status,
            "available": item.available,
            "pinned": item.pinned,
            "enabled": item.enabled,
            "notify_requested": item.notify_requested,
        }
        for item in await service.arranged()
    ]


@tool("write")
async def modules_set_preferences(
    ctx: ActorContext,
    key: ModuleKey,
    pinned: Annotated[bool | None, Field(description="true = favourite, false = not")] = None,
    enabled: Annotated[
        bool | None, Field(description="true = show on the page, false = hide")
    ] = None,
) -> ModuleView:
    """Favourite or unfavourite a module, or show or hide it on the user's page."""
    return await _service(ctx).update_preferences(
        key, PreferencesIn(pinned=pinned, enabled=enabled)
    )


@tool("write")
async def modules_set_interest(
    ctx: ActorContext,
    key: ModuleKey,
    wanted: Annotated[bool, Field(description="true = notify me at launch, false = stop")],
) -> ModuleView:
    """Turn the launch notice for a coming-soon module on or off."""
    return await _service(ctx).set_interest(key, wanted=wanted)


@tool("client")
async def modules_open(ctx: ActorContext, key: ModuleKey) -> None:
    """Open a module for the user: its pages when available, otherwise its details."""
    _service(ctx).registry.get(key)
